"""
Safely commit + push trader_state.json / trader_log.json after a bot run.

Replaces the old git stash / pull --rebase / stash pop sequence, which
produced silent, catastrophic data loss three times: when two concurrent
runs (repository_dispatch + the schedule fallback, which can start within
seconds of each other despite concurrency: cancel-in-progress: false — a
known GitHub Actions edge case) both hit a stash-pop conflict, git wrote
literal <<<<<<< / ======= / >>>>>>> conflict markers into the committed
file, and load_log()/load_state()'s broad exception handling silently
treated the resulting invalid JSON as "empty" on the very next run,
overwriting the entire history.

This script avoids git's line-based merge entirely for these two files
and instead does an application-level, JSON-aware merge:

  trader_log.json (append-only): on push rejection, re-fetch the fresh
  remote log and re-append just the entries THIS run added — safe,
  because independent appends to the tail of an array always merge
  cleanly regardless of order.

  trader_state.json (single mutable object, not append-only): split into
  "decision" fields (position/signal/entry/trade_count — the fields the
  trading logic actually decides) and "telemetry" fields (last_run_utc,
  last_known_pnl, etc — snapshot values that are expected to differ
  slightly between two runs seconds apart). Evidence from the three real
  incidents showed decision fields always matched between colliding runs
  (both computed the same trade decision from the same market data) and
  only telemetry fields genuinely conflicted. So: if decision fields
  match, take the fresh remote's decision fields (no information lost)
  and this run's own telemetry. If they ever genuinely differ (not
  observed in practice, but handled defensively), last-writer-wins by
  last_run_utc and a Telegram alert fires so a human checks — this is a
  real tradeoff, not a complete merge, and is treated as such rather than
  silently picking a side.

  peak_pnl/peak_pnl_time sit between the two categories: decision-relevant
  (trailing stop) but churning every cycle while in profit, so colliding
  runs routinely disagree on them by fractions of a cent. They merge as
  max(local, remote) when the decision fields agree (a peak is monotone
  for the life of a position) and are excluded from the conflict check —
  see the 2026-07-09 false-alarm ping-pong (3 alerts over a $0.00002 peak).
"""
import json, os, subprocess, sys, time
from datetime import datetime

STATE_FILE = "trader_state.json"
LOG_FILE   = "trader_log.json"
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
MAX_RETRIES = 5

DECISION_FIELDS = [
    "position_open", "current_signal", "entry_btc_size", "entry_eth_size",
    "entry_time_utc", "entry_btc_price", "entry_eth_price", "entry_equity",
    "trade_count", "open_signal",
    "trail_active", "trail_flip_active", "last_flip_signal",
]
# peak_pnl is decision-relevant (drives the trailing stop) but, unlike the
# fields above, it churns every cycle while a position is in profit — two
# overlapping runs will routinely disagree on it by fractions of a cent,
# which used to trip the decision-conflict alert in a retry ping-pong
# (3 false alarms on 2026-07-09 over a $0.00002 peak). A peak is monotone
# non-decreasing for the lifetime of a position, so when the rest of the
# decision fields agree, max(local, remote) is always the correct merge.
PEAK_FIELDS = ["peak_pnl", "peak_pnl_time"]
TELEMETRY_FIELDS = [
    "last_run_utc", "last_known_pnl", "last_equity", "last_avail",
    "last_unrealized_pnl", "equity_updated_utc", "last_lag_alert_utc",
    "last_api_fail_utc",
]

def merge_state(local_state, remote_state):
    """Application-level merge of two colliding trader_state.json versions.
    Returns (merged_state, conflict). conflict is None when the merge is
    clean, else the (local_snapshot, remote_snapshot, used_local) triple
    for the alert. Peak fields merge as max() when decisions agree; on a
    genuine decision conflict the winning side is kept wholesale (a peak
    from a divergent state, e.g. one side already closed and reset it,
    must not be resurrected across that divergence)."""
    decision_match = all(
        remote_state.get(k) == local_state.get(k) for k in DECISION_FIELDS
    )
    if decision_match:
        merged = dict(remote_state)
        for k in TELEMETRY_FIELDS:
            if k in local_state:
                merged[k] = local_state[k]
        l_peak = float(local_state.get("peak_pnl") or 0.0)
        r_peak = float(remote_state.get("peak_pnl") or 0.0)
        winner = local_state if l_peak > r_peak else remote_state
        for k in PEAK_FIELDS:
            if k in winner:
                merged[k] = winner[k]
        return merged, None

    try:
        remote_ts = datetime.fromisoformat(remote_state.get("last_run_utc", ""))
        local_ts  = datetime.fromisoformat(local_state.get("last_run_utc", ""))
        use_local = local_ts > remote_ts
    except Exception:
        use_local = False
    merged = local_state if use_local else remote_state
    return merged, (
        {k: local_state.get(k) for k in DECISION_FIELDS},
        {k: remote_state.get(k) for k in DECISION_FIELDS},
        use_local,
    )

def tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  Telegram send failed: {e}")

def run_git(*args, check=True):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r

def show_at_ref(ref, path):
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None

def main():
    # Baseline = what was already committed (HEAD) before this workflow run
    # touched anything locally. Anything beyond this in the local file is
    # what THIS run's python btcd_trader.py added.
    baseline_log_raw = show_at_ref("HEAD", LOG_FILE)
    baseline_log = json.loads(baseline_log_raw) if baseline_log_raw else []

    with open(LOG_FILE) as f:
        local_log = json.load(f)
    new_entries = local_log[len(baseline_log):]

    with open(STATE_FILE) as f:
        local_state = json.load(f)

    for attempt in range(1, MAX_RETRIES + 1):
        run_git("add", STATE_FILE, LOG_FILE)
        staged_diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if staged_diff.returncode == 0:
            print("  No changes to commit.")
            return 0

        run_git("commit", "-m", "bot: update trader state [skip ci]")
        push = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push.returncode == 0:
            print(f"  Pushed successfully (attempt {attempt})")
            return 0

        print(f"  Push rejected (attempt {attempt}/{MAX_RETRIES}): {push.stderr.strip()[:300]}")
        run_git("fetch", "origin", "main")

        remote_log_raw   = show_at_ref("origin/main", LOG_FILE)
        remote_state_raw = show_at_ref("origin/main", STATE_FILE)
        try:
            remote_log = json.loads(remote_log_raw)
        except Exception as e:
            print(f"  ABORT: remote {LOG_FILE} is not valid JSON ({e}) — cannot safely merge")
            tg(f"🚨 save_state.py: remote {LOG_FILE} failed to parse — manual intervention needed.")
            return 1
        try:
            remote_state = json.loads(remote_state_raw)
        except Exception as e:
            print(f"  ABORT: remote {STATE_FILE} is not valid JSON ({e}) — cannot safely merge")
            tg(f"🚨 save_state.py: remote {STATE_FILE} failed to parse — manual intervention needed.")
            return 1

        # Discard our failed local commit attempt, reset to the fresh remote base
        run_git("reset", "--hard", "origin/main")

        # Re-apply log: fresh remote + our own new entries appended
        merged_log = remote_log + new_entries
        with open(LOG_FILE, "w") as f:
            json.dump(merged_log, f, indent=2)

        # Re-apply state: decision-vs-telemetry merge (peaks merge as max)
        merged_state, conflict = merge_state(local_state, remote_state)
        if conflict:
            local_snap, remote_snap, use_local = conflict
            tg("⚠️ State decision conflict between concurrent runs — used "
               f"{'local' if use_local else 'remote'} version, manual review recommended.\n"
               f"local: {json.dumps(local_snap)}\n"
               f"remote: {json.dumps(remote_snap)}")

        with open(STATE_FILE, "w") as f:
            json.dump(merged_state, f, indent=2)

        time.sleep(2)

    print(f"  ERROR: exhausted {MAX_RETRIES} retries — giving up this cycle, next cycle will retry fresh")
    tg(f"⚠️ save_state.py: failed to push after {MAX_RETRIES} retries this cycle.")
    return 1

if __name__ == "__main__":
    sys.exit(main())
