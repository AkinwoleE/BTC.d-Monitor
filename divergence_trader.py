#!/usr/bin/env python3
"""
Divergence trader — executes the backtested rule set on Decibel.

Runs as a workflow step AFTER divergence_monitor.py. Consumes divergences.json;
never detects patterns itself.

Rule set (derived from the 13-trade 2-pt backfill, 2026-07-29):
  entry      market order at signal confirmation ($DIV_POSITION_USD notional)
  take-profit +$34 per $1000 (3.4% move), native exchange TP order
  stop-loss   -$20 per $1000 (2.0% move), native exchange SL order
  time-exit   close after 48h if neither TP nor SL fired
  overlap     same-direction signal while open: skip; opposite: close & flip

Gating: trades ONLY when DIV_TRADING_ENABLED=true AND Decibel creds are set.
DIV_DRY_RUN=true logs+alerts every decision without placing orders.

DIV_TRADE_TF (default "1h,2h") restricts which signal timeframes this trader
will ACT on — comma-separated. Detection/alerting in divergence_monitor.py is
unaffected either way; this only gates entries here. Added 2026-08-19 after
a 3-year backtest showed the 1h/2-pt combination losing money at scale while
2h stayed positive across almost every 6-month window — set to "2h" on both
accounts to stop trading 1h without losing 1h visibility on the dashboard.

DIV_BLOCK_DAYS (default "", comma-separated 3-letter UTC weekday abbrevs,
e.g. "Sat,Sun") blocks NEW ENTRIES only on the listed days — a signal that
confirms on a blocked day is simply never traded (not deferred to the next
allowed day). Does NOT affect management of an already-open position: SL/TP/
48h-exit checks run every day regardless, so a position opened before a
blocked day is never left unmanaged. Does NOT affect the manual-trade
workflow_dispatch path — that's an explicit human action, exempt by design.
Added 2026-08-19 after the 3-year backtest showed Saturday/Sunday entries
underperforming weekday ones on the 2h timeframe (the only one now live).

Reliability of entries (the rules above are unchanged):
  A 2h signal can still be entered up to 18h after confirmation, so a late
  or missed run does not skip it. 1h stays at 2h. Inside that window the
  entry is skipped — and alerted — if price has already run more than halfway
  to the target, or through the full stop. The order is checkpointed before
  it is sent, retried on transient errors, and verified against the live
  position. One signal produces at most one order. A fill the previous run
  did not get to record is picked up and managed (stop, target, 48h). An
  unknown position with no matching pending order is still left alone.

Multi-account: set TRADER_ACCOUNT to run this same script against a second
person's Decibel account (e.g. a friend's), fully isolated from the primary
account — separate state/log/equity files (suffixed by account name), and
every Telegram message tagged with the account so the two are never
ambiguous in a shared chat. The account's credentials/sizing/enable-flag are
supplied by the CALLER (the GitHub workflow maps that account's secrets onto
these same generic env var names for that step) — this file never hardcodes
a second set of credential names, so adding a third account later is a
workflow-only change.

State: divergence_trader_state{_account}.json (fails LOUD on corruption —
never silently resets; a bad file is restored from .bak or left untouched).
Log:   divergence_trades{_account}.json (append-only events; same rule).
"""
import os, json, time, subprocess, requests
from datetime import datetime, timezone

from divergence_reliability import (
    MAX_ENTRY_AGE_SEC,
    PENDING_MAX_WAIT_SEC,
    DurableJsonError,
    TransientError,
    atomic_write_json,
    classify_entry,
    flush_git_state,
    is_transient_error,
    live_entry_price,
    live_matches,
    payload_is_transient,
    pending_resolution,
    plan_order,
    read_json_durable,
    retry_call,
    should_alert_skip,
)

BASE    = os.path.dirname(os.path.abspath(__file__))
ACCOUNT = (os.environ.get("TRADER_ACCOUNT", "") or "primary").strip()
SUFFIX  = "" if ACCOUNT.lower() == "primary" else f"_{ACCOUNT.lower()}"
TAG     = f"[{ACCOUNT}] "

DIV_FILE   = os.path.join(BASE, "divergences.json")               # shared signal source — never suffixed
STATE_FILE = os.path.join(BASE, f"divergence_trader_state{SUFFIX}.json")
LOG_FILE   = os.path.join(BASE, f"divergence_trades{SUFFIX}.json")
EQ_FILE    = os.path.join(BASE, f"divergence_equity{SUFFIX}.json")

def env_num(name, default, cast=float):
    """GitHub Actions injects '' for undefined ${{ vars.X }} — treat as unset."""
    v = os.environ.get(name, "").strip()
    return cast(v) if v else cast(default)

ENABLED      = os.environ.get("DIV_TRADING_ENABLED", "").lower() == "true"
DRY_RUN      = os.environ.get("DIV_DRY_RUN", "").lower() == "true"
POSITION_USD = env_num("DIV_POSITION_USD", 1000)
TP_USD       = env_num("DIV_TP_USD", 34)    # per POSITION_USD
SL_USD       = env_num("DIV_SL_USD", 20)
MAX_HOURS    = env_num("DIV_MAX_HOURS", 48)
LEVERAGE     = env_num("DIV_LEVERAGE", 20, int)
SLIPPAGE     = env_num("SLIPPAGE", 0.5)
# one-off manual test trade (workflow_dispatch only — never set on the hourly
# cron) — bypasses signal detection entirely, places a single real order
# using the normal open_position() path so it's picked up and managed
# (TP/SL/48h-exit) by every subsequent run exactly like a signal-driven trade
MANUAL_SIDE     = os.environ.get("MANUAL_SIDE", "").strip().lower()   # "long" or "short"
MANUAL_SIZE_USD = env_num("MANUAL_SIZE_USD", 0)
SYMBOL       = "BTC/USD"
TRADE_TF     = {t.strip() for t in os.environ.get("DIV_TRADE_TF", "1h,2h").split(",") if t.strip()}
BLOCK_DAYS   = {d.strip()[:3].title() for d in os.environ.get("DIV_BLOCK_DAYS", "").split(",") if d.strip()}
GRACE_SEC    = 600                                # reconciliation grace after a tracked entry

DEC_PRIVATE_KEY = os.environ.get("DECIBEL_PRIVATE_KEY", "")
DEC_SUB         = os.environ.get("DECIBEL_SUBACCOUNT", "")
DEC_NODE_KEY    = os.environ.get("DECIBEL_NODE_API_KEY", "")
DEC_GAS_KEY     = os.environ.get("DECIBEL_GAS_STATION_API_KEY", "")
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")

SKIP_TEXT = {
    "too_old": "the signal is outside the entry window, so it was not chased",
    "price_ran_ahead": "price already moved more than halfway to the target",
    "price_ran_against": "price already moved through the stop",
    "missing_price": "the signal has no confirmation price to check",
}

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg(msg):
    msg = TAG + msg   # every alert is tagged with the account — never ambiguous in a shared chat
    if not BOT_TOKEN or not CHAT_ID:
        print(f"  [no telegram] {msg}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"  Telegram failed: {e}")

def checkpoint():
    """Push state/log/signals when running in GitHub Actions.

    The runner disk is discarded if the job is killed. A push made before the
    order, and again once the fill is recorded, is what the next run sees.
    """
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return False
    return flush_git_state(
        [STATE_FILE, LOG_FILE, DIV_FILE],
        enabled=True,
        cwd=BASE,
        alert=lambda m: tg(f"🚨 Divergence checkpoint failed: {m}"),
    )

# ── Decibel CLI (same JSON-RPC-over-npx mechanism as btcd_trader.py) ──────────
def cli_env():
    e = os.environ.copy()
    e["DECIBEL_NETWORK"]            = "mainnet"
    e["DECIBEL_PRIVATE_KEY"]        = DEC_PRIVATE_KEY
    e["DECIBEL_SUBACCOUNT_ADDRESS"] = DEC_SUB
    e["DECIBEL_NODE_API_KEY"]       = DEC_NODE_KEY
    if DEC_GAS_KEY: e["DECIBEL_GAS_STATION_API_KEY"] = DEC_GAS_KEY
    return e

def install_cli():
    """Warm npx's package cache before the real calls: a fresh GitHub Actions
    runner has nothing cached, so the FIRST npx invocation each run has to
    resolve+download @decibeltrade/cli from scratch, which has been
    intermittently exceeding run_cli()'s 90s budget (2026-09-04 incident —
    live_snapshot failing on ~13 of 15 consecutive runs, alerting every 6h).
    btcd_trader.py already had this exact fix; this just ports it over.
    Best-effort: if the warm-up itself times out, run_cli() proceeds anyway
    with its own full timeout budget rather than failing the whole cycle."""
    print("  Caching @decibeltrade/cli via npx...")
    try:
        r = subprocess.run(
            ["npx", "-y", "--package", "@decibeltrade/cli", "decibel-mcp", "--version"],
            capture_output=True, text=True, timeout=60, env=cli_env())
        print(f"  Cache result: {(r.stdout+r.stderr).strip()[:80]}")
    except subprocess.TimeoutExpired:
        print("  WARNING: install_cli timed out (60s) — skipping cache warm, run_cli will proceed independently")
    except Exception as e:
        print(f"  WARNING: install_cli failed ({e}) — skipping cache warm, run_cli will proceed independently")

def run_cli(action, params):
    rpc = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": action, "arguments": params}}) + "\n"
    result = subprocess.run(["npx", "-y", "--package", "@decibeltrade/cli", "decibel-mcp"],
                            input=rpc, capture_output=True, text=True, timeout=90, env=cli_env())
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("(node:") or "MaxListenersExceeded" in line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 1:
            if "error" in msg:
                err = RuntimeError(f"MCP error: {msg['error']}")
                if is_transient_error(err):
                    raise TransientError(str(err))
                raise err
            for item in msg.get("result", {}).get("content", []):
                if item.get("type") == "text":
                    try: return json.loads(item["text"])
                    except Exception: return {"result": item["text"]}
            return msg.get("result", {})
    raise TransientError(f"No JSON-RPC response for {action}: {result.stdout[:200]} / {result.stderr[:200]}")

def order_ok(r):
    """True only when an order result looks like a success (btcd lesson: never
    record an OPEN off an unvalidated result)."""
    if not isinstance(r, dict): return False
    if r.get("error"): return False
    if r.get("success") is False: return False
    return True

_MARKETS = {}
def market_info():
    if not _MARKETS:
        res = run_cli("get_markets", {})
        for m in res.get("markets", []):
            _MARKETS[m["name"]] = {"address": m["address"],
                                   "min_size": m["minSize"] / (10 ** m["sizeDecimals"])}
    return _MARKETS.get(SYMBOL)

def live_snapshot():
    """(btc_position|None, equity, api_available) — with minSize dust floor."""
    try:
        info = market_info()
        pos = run_cli("get_positions", {}).get("positions", [])
        bal = run_cli("get_balances", {"subaccountAddress": DEC_SUB})
        equity = float(bal.get("perpEquityBalance", 0))
        if equity <= 0.01:
            # 2026-07-09 / 2026-09-01 incident class: Decibel's API occasionally
            # returns a successful-looking but empty/zero balance transiently
            # (see implausible(), which guards the trading path against this).
            # This wicked the equity graph to $0 and back on 2026-09-01 because
            # record_equity() runs before that guard is even reached. A single
            # immediate retry distinguishes a real near-zero balance (which
            # reproduces) from a one-off glitch (which doesn't), without
            # risking ever permanently masking a genuine crash.
            time.sleep(2)
            bal2 = run_cli("get_balances", {"subaccountAddress": DEC_SUB})
            equity2 = float(bal2.get("perpEquityBalance", 0))
            if equity2 > 0.01:
                print(f"  live_snapshot: retry recovered equity ${equity2:.2f} "
                      f"(first read ${equity:.2f} — transient glitch, not recorded)")
                equity = equity2
        btc = None
        for p in pos:
            if info and p.get("market") == info["address"]:
                if abs(float(p.get("size", 0))) >= (info["min_size"] or 0):
                    btc = p
        return btc, equity, True
    except Exception as e:
        print(f"  live_snapshot failed: {e}")
        return None, 0.0, False

def implausible(state_open, live_pos, equity):
    # 2026-07-09 incident class: successful-looking but EMPTY snapshot
    return bool(state_open and live_pos is None and equity <= 0.01)

def dec_price():
    def read():
        r = run_cli("get_price", {"symbol": SYMBOL})
        for k in ("price", "markPrice", "lastPrice", "indexPrice"):
            if r.get(k) is not None:
                return float(r[k])
        raise RuntimeError(f"no price in {r}")
    return retry_call(read, attempts=3, base_delay=1.0)

def cancel_all_tpsl():
    try:
        r = run_cli("get_tp_sl", {"symbol": SYMBOL})
        orders = r.get("orders", r.get("tpSlOrders", [])) or []
        for o in orders:
            oid = o.get("orderId") or o.get("id")
            if oid:
                run_cli("cancel_tp_sl", {"symbol": SYMBOL, "orderId": str(oid)})
                print(f"  cancelled tp/sl {oid}")
    except Exception as e:
        print(f"  cancel_all_tpsl: {e}")

def has_tpsl():
    """True / False when the check succeeded, None when it could not be read.

    None must not be treated as 'no orders' — that used to place a second
    bracket or software-close a position that was already protected.
    """
    try:
        r = run_cli("get_tp_sl", {"symbol": SYMBOL})
        return bool(r.get("orders", r.get("tpSlOrders", [])) or [])
    except Exception as e:
        print(f"  has_tpsl failed: {e}")
        return None

# ── state / log ───────────────────────────────────────────────────────────────
def load_state():
    try:
        st = read_json_durable(STATE_FILE, default={"position": None, "acted": {}})
    except DurableJsonError as e:
        tg(f"🚨 divergence_trader: STATE FILE CORRUPT and no backup ({e}) — refusing to run. "
           f"The file was not replaced.")
        raise
    if not isinstance(st, dict):
        tg("🚨 divergence_trader: state file is not an object — refusing to run.")
        raise DurableJsonError("state is not an object")
    if "acted" in st and not isinstance(st.get("acted"), dict):
        tg("🚨 divergence_trader: state 'acted' is unreadable — refusing to run so a signal "
           "is not entered twice.")
        raise DurableJsonError("acted is not an object")
    st.setdefault("position", None)
    st.setdefault("acted", {})
    if not isinstance(st.get("skip_noted"), dict):
        st["skip_noted"] = {}
    if st.get("pending_open") is not None and not isinstance(st.get("pending_open"), dict):
        tg("🚨 divergence_trader: pending_open is unreadable — refusing to run.")
        raise DurableJsonError("pending_open is not an object")
    return st

def save_state(st):
    atomic_write_json(STATE_FILE, st, indent=1)

def record_equity(equity):
    """Hourly (or on >0.5% move) equity snapshots for the dashboard's graph."""
    try:
        try:
            hist = read_json_durable(EQ_FILE, default=[])
        except DurableJsonError as e:
            print(f"  record_equity: not overwriting corrupt file ({e})")
            return
        if not isinstance(hist, list):
            print("  record_equity: file is not a list — not overwriting")
            return
        now = int(time.time())
        if hist:
            last = hist[-1]
            moved = abs(equity - last["equity"]) >= max(0.005 * max(last["equity"], 1.0), 0.01)
            if now - last["ts"] < 3300 and not moved:
                return
        hist.append({"ts": now, "equity": round(equity, 2)})
        atomic_write_json(EQ_FILE, hist[-5000:], indent=None)
    except Exception as e:
        print(f"  record_equity: {e}")

def log_event(ev):
    try:
        try:
            log = read_json_durable(LOG_FILE, default=[])
        except DurableJsonError as e:
            tg(f"🚨 trade log is corrupt and was not overwritten ({e}). "
               f"This {ev.get('action')} event was not appended.")
            return
        if not isinstance(log, list):
            tg("🚨 trade log is not a list — refusing to overwrite it.")
            return
        ev["timestamp"] = datetime.now(timezone.utc).isoformat()
        log.append(ev)
        atomic_write_json(LOG_FILE, log, indent=1)
    except Exception as e:
        print(f"  log_event: {e}")

def load_episodes():
    """Signal list. A corrupt file is restored from .bak or left untouched.

    Returns [] when the file cannot be read so an open position is still
    managed this cycle. Never writes an empty episode list over the original.
    """
    try:
        data = read_json_durable(DIV_FILE, default={"episodes": []})
    except DurableJsonError as e:
        tg(f"🚨 divergences.json unreadable ({e}). Open positions are still managed; "
           f"new entries wait until the file is readable.")
        return []
    if not isinstance(data, dict):
        tg("🚨 divergences.json has an unexpected shape. New entries wait.")
        return []
    eps = data.get("episodes") or []
    if not isinstance(eps, list):
        tg("🚨 divergences.json episodes are unreadable. New entries wait.")
        return []
    return eps

# ── trade actions ─────────────────────────────────────────────────────────────
def tp_sl_prices(direction, entry):
    frac_tp, frac_sl = TP_USD / POSITION_USD, SL_USD / POSITION_USD
    if direction == "bullish":   # long
        tp, sl = entry * (1 + frac_tp), entry * (1 - frac_sl)
        sl_lim = sl * 0.995      # limit through the trigger so the stop fills
    else:                        # short
        tp, sl = entry * (1 - frac_tp), entry * (1 + frac_sl)
        sl_lim = sl * 1.005
    return round(tp, 1), round(sl, 1), round(sl_lim, 1)

def _px(v):
    return f"{v:,.0f}" if isinstance(v, (int, float)) else "?"

def place_protection(direction, entry):
    """Put the native bracket on, unless one is already there."""
    tp, sl, sl_lim = tp_sl_prices(direction, entry)
    status = has_tpsl()
    if status is True:
        print(f"  tp/sl already present (tp={tp} sl={sl})")
        return True, tp, sl
    if status is None:
        print("  tp/sl check failed — not placing another bracket this cycle")
        return False, tp, sl
    params = {"symbol": SYMBOL,
              "tpTriggerPrice": tp, "tpLimitPrice": tp,
              "slTriggerPrice": sl, "slLimitPrice": sl_lim}

    def on_transient(_err, _i):
        status = has_tpsl()
        if status is True:
            return {"_already": True}
        if status is None:
            return {"_unknown": True}
        return None

    try:
        r = retry_call(lambda: run_cli("place_tp_sl", params),
                       attempts=3, base_delay=2.0, on_transient=on_transient)
    except Exception as e:
        print(f"  place_tp_sl failed: {e}")
        return False, tp, sl
    if isinstance(r, dict) and r.get("_already"):
        return True, tp, sl
    if isinstance(r, dict) and r.get("_unknown"):
        print("  tp/sl status unknown after a send error — not placing another bracket")
        return False, tp, sl
    ok = order_ok(r)
    print(f"  tp/sl: tp={tp} sl={sl} ok={ok} {json.dumps(r)[:120]}")
    return ok, tp, sl

def _clear_pending(st, episode_id=None):
    pending = st.get("pending_open")
    if not pending:
        return
    if episode_id is None or pending.get("episode_id") == episode_id:
        st["pending_open"] = None

def _entry_from_result(r, fallback):
    if isinstance(r, dict):
        for key in ("fillPrice", "avgPrice", "price"):
            if r.get(key) not in (None, ""):
                try:
                    return float(r[key])
                except (TypeError, ValueError):
                    pass
    return fallback

def _finish_open(st, pending, entry, recovered):
    """Record the position and arm stop / target / time-exit. Clears pending."""
    direction = pending["direction"]
    side = pending.get("side") or ("long" if direction == "bullish" else "short")
    try:
        tpsl_ok, tp, sl = place_protection(direction, entry)
    except Exception as e:
        print(f"  place_tp_sl raised: {e}")
        tp, sl, _sl_lim = tp_sl_prices(direction, entry)
        tpsl_ok = False
    now_ts = int(time.time())
    pos = {"episode_id": pending["episode_id"], "direction": direction,
           "size": pending["size"], "entry_price": entry,
           "entry_ts": int(pending.get("ts") or now_ts),
           "entry_equity": pending.get("entry_equity"),
           "tp_price": tp, "sl_price": sl, "tp_sl_ok": tpsl_ok}
    if not tpsl_ok:
        pos["tpsl_alert_ts"] = now_ts
    st["position"] = pos
    st["acted"][pending["episode_id"]] = now_ts
    st["pending_open"] = None
    save_state(st)
    log_event({"action": "OPEN", "episode_id": pending["episode_id"], "direction": direction,
               "side": side, "size": pending["size"], "entry_price": entry,
               "equity": pending.get("entry_equity"), "tp_price": tp, "sl_price": sl,
               "tp_sl_ok": tpsl_ok, "recovered": recovered})
    checkpoint()
    warn = "" if tpsl_ok else "\n🚨 TP/SL placement FAILED — will retry every run. Software exit runs only when the exchange check succeeds and shows no orders."
    size_usd = pending.get("size_usd") or POSITION_USD
    if recovered:
        tg(f"🛟 <b>Divergence bot recovered an open {side.upper()} {SYMBOL}</b>\n"
           f"The previous run sent this order and stopped before it could track it.\n"
           f"Signal: {pending['episode_id']}\n"
           f"Entry ~{_px(entry)} · TP {_px(tp)} · SL {_px(sl)} · "
           f"time-exit {MAX_HOURS:.0f}h from the original order.{warn}")
    else:
        arrow = "🔼" if side == "long" else "🔻"
        tg(f"{arrow} <b>Divergence bot OPENED {side.upper()} {SYMBOL}</b>\n"
           f"Signal: {pending['episode_id']}\nSize: {pending['size']} (${size_usd:.0f} @ {LEVERAGE}x)\n"
           f"Entry ~{_px(entry)} · TP {_px(tp)} · SL {_px(sl)} · time-exit {MAX_HOURS:.0f}h{warn}")

def _fail_order(st, pending, ambiguous, detail):
    """Record a failed send. Ambiguous failures must not be sent again."""
    if ambiguous:
        pending["order_sent"] = True
        msg = (f"🚨 Divergence OPEN for {pending['episode_id']} did not confirm "
               f"({str(detail)[:180]}). Not sending a second order. "
               f"The next runs will manage it if the position appears.")
    else:
        pending["order_sent"] = False
        msg = (f"🚨 Divergence OPEN FAILED on {pending['episode_id']}: {str(detail)[:200]}. "
               f"Will retry while the signal is still valid.")
    pending["last_error"] = str(detail)[:300]
    st["pending_open"] = pending
    save_state(st)
    checkpoint()
    tg(msg)

def _await_fill(side, size, attempts=3):
    last = (None, None, False)
    for i in range(attempts):
        live, equity, ok = live_snapshot()
        last = (live, equity, ok)
        if ok and live_matches(live, side, size):
            return live, equity, True
        if i < attempts - 1:
            time.sleep(2)
    return last[0], last[1], False

def open_position(st, ep, equity, size_usd=None):
    try:
        _open_position(st, ep, equity, size_usd)
    except Exception as e:
        tg(f"🚨 Divergence entry error on {ep.get('id')}: {e}")
        print(f"  open_position error: {e}")

def _open_position(st, ep, equity, size_usd=None):
    size_usd = POSITION_USD if size_usd is None else size_usd
    direction = ep["direction"]
    side = "long" if direction == "bullish" else "short"
    if ep["id"] in st.get("acted", {}):
        _clear_pending(st, ep["id"])
        return
    existing = st.get("pending_open")
    if existing and existing.get("order_sent"):
        print(f"  not opening {ep['id']} — order for {existing.get('episode_id')} was already sent")
        return
    need = size_usd / LEVERAGE * 1.15
    if equity < need:
        tg(f"⚠️ Divergence trade SKIPPED — insufficient margin: equity ${equity:.2f} &lt; ${need:.2f} "
           f"needed for ${size_usd:.0f} @ {LEVERAGE}x. Deposit to enable.")
        st["acted"][ep["id"]] = int(time.time())
        _clear_pending(st, ep["id"])
        return
    price = dec_price()
    size = round(size_usd / price, 5)
    info = market_info()
    if info and size < info["min_size"]:
        tg(f"⚠️ Divergence trade SKIPPED — size {size} below market min {info['min_size']}")
        st["acted"][ep["id"]] = int(time.time())
        _clear_pending(st, ep["id"])
        return
    if DRY_RUN:
        tg(f"🧪 DRY RUN — would OPEN {side.upper()} {SYMBOL} sz={size} @ ~{price:,.0f} on {ep['id']}")
        st["acted"][ep["id"]] = int(time.time())
        _clear_pending(st, ep["id"])
        return
    try:
        run_cli("set_leverage", {"symbol": SYMBOL, "leverage": LEVERAGE, "marginType": "cross"})
    except Exception as e:
        print(f"  set_leverage warning: {e}")
    pending = {"episode_id": ep["id"], "direction": direction, "side": side,
               "size": size, "size_usd": size_usd, "signal_price": price,
               "confirm_price": ep.get("confirm_price"), "ts": int(time.time()),
               "order_sent": False, "entry_equity": equity}
    st["pending_open"] = pending
    save_state(st)
    checkpoint()
    pending["order_sent"] = True
    st["pending_open"] = pending
    save_state(st)
    checkpoint()

    def matched_fill():
        live, eq, ok = live_snapshot()
        if ok and live_matches(live, side, size):
            return live, eq
        return None

    def on_transient(_err, _i):
        found = matched_fill()
        if not found:
            return None
        live, eq = found
        return {"_recovered_fill": True, "live": live, "equity": eq}

    send_n = {"n": 0}

    def attempt():
        send_n["n"] += 1
        # Only retries look for a fill first. The first attempt must not treat
        # an existing manual position of a similar size as our order.
        if send_n["n"] > 1:
            found = matched_fill()
            if found:
                live, _eq = found
                return {"_recovered_fill": True, "live": live}
        r = run_cli("place_market_order", {"symbol": SYMBOL, "side": side, "size": size,
                                           "slippage": SLIPPAGE, "reduceOnly": False})
        if order_ok(r):
            return r
        if payload_is_transient(r):
            raise TransientError(json.dumps(r)[:300])
        return r

    try:
        r = retry_call(attempt, attempts=3, base_delay=2.0, on_transient=on_transient)
    except Exception as e:
        found = matched_fill()
        if found:
            r = {"_recovered_fill": True, "live": found[0]}
        else:
            _fail_order(st, pending, is_transient_error(e), e)
            return

    if isinstance(r, dict) and r.get("_recovered_fill"):
        entry = live_entry_price(r.get("live")) or price
        _finish_open(st, pending, entry, recovered=False)
        return
    if not order_ok(r):
        _fail_order(st, pending, False, json.dumps(r)[:300] if isinstance(r, dict) else r)
        return
    entry = _entry_from_result(r, price)
    live, _eq, visible = _await_fill(side, size)
    if visible:
        entry = live_entry_price(live) or entry
        _finish_open(st, pending, entry, recovered=False)
        return
    pending["order_sent"] = True
    st["pending_open"] = pending
    save_state(st)
    checkpoint()
    tg(f"🚨 Order for {ep['id']} was accepted but the {SYMBOL} position is not visible yet. "
       f"Not placing a second order. The next runs will manage it if it appears.")

def close_position_now(st, reason):
    pos = st["position"]
    if DRY_RUN:
        tg(f"🧪 DRY RUN — would CLOSE ({reason})")
        st["position"] = None
        return True
    cancel_all_tpsl()

    def on_transient(_err, _i):
        live, equity, ok = live_snapshot()
        if ok and live is None:
            return {"success": True, "_already_flat": True, "_equity": equity}
        return None

    try:
        r = retry_call(
            lambda: run_cli("close_position", {"symbol": SYMBOL, "slippage": SLIPPAGE}),
            attempts=3, base_delay=2.0, on_transient=on_transient)
    except Exception as e:
        tg(f"🚨 Divergence CLOSE FAILED ({reason}): {e} — will retry next run.")
        return False
    if not order_ok(r):
        tg(f"🚨 Divergence CLOSE FAILED ({reason}): {json.dumps(r)[:200]} — will retry next run.")
        return False
    time.sleep(2)
    if isinstance(r, dict) and r.get("_equity") is not None:
        equity, ok = r["_equity"], True
    else:
        _, equity, ok = live_snapshot()
    pnl = (equity - pos["entry_equity"]) if ok and pos.get("entry_equity") is not None else None
    log_event({"action": "CLOSE", "episode_id": pos["episode_id"], "reason": reason,
               "direction": pos["direction"], "entry_price": pos["entry_price"],
               "pnl": pnl, "held_hours": round((time.time() - pos["entry_ts"]) / 3600, 1)})
    ptxt = f"{pnl:+.2f}" if pnl is not None else "?"
    tg(f"✅ <b>Divergence bot CLOSED</b> ({reason})\n"
       f"{pos['direction']} from {pos['entry_price']:,.0f}, held "
       f"{(time.time()-pos['entry_ts'])/3600:.1f}h · equity-based PnL ${ptxt}")
    st["position"] = None
    save_state(st)
    checkpoint()
    return True

def note_skip(st, ep, reason, now):
    """Remember a signal we will not enter. Alert once, and only if it is recent."""
    eid = ep.get("id")
    if not eid:
        return
    noted = st.setdefault("skip_noted", {})
    if eid in noted or eid in st.get("acted", {}):
        return
    noted[eid] = now
    if not should_alert_skip(ep.get("confirmed_ts"), now):
        return
    tg(f"⏭ Divergence bot skipped {eid}: {SKIP_TEXT.get(reason, reason)}.")
    log_event({"action": "SKIP", "episode_id": eid, "reason": reason})

def manual_trade(st, equity):
    """One-off test trade requested via workflow_dispatch inputs, independent
    of DIV_TRADING_ENABLED — an explicit ad-hoc action, not the strategy."""
    if st.get("position"):
        tg(f"⚠️ Manual trade SKIPPED — a position is already open on this account "
           f"({st['position']['direction']} from {st['position']['entry_price']:,.0f}). "
           f"Close it first.")
        return
    if st.get("pending_open"):
        tg(f"⚠️ Manual trade SKIPPED — an order is already pending "
           f"({st['pending_open'].get('episode_id')}).")
        return
    if MANUAL_SIZE_USD <= 0:
        tg("⚠️ Manual trade SKIPPED — MANUAL_SIZE_USD not set or <= 0."); return
    direction = "bullish" if MANUAL_SIDE == "long" else "bearish"
    ep = {"id": f"MANUAL-{int(time.time())}", "direction": direction}
    print(f"  [{ACCOUNT}] MANUAL TRADE: {MANUAL_SIDE} ${MANUAL_SIZE_USD:.0f} @ {LEVERAGE}x")
    open_position(st, ep, equity, size_usd=MANUAL_SIZE_USD)
    save_state(st)
    checkpoint()

def _resolve_pending(st, live_pos, equity, now):
    """Adopt, wait, give up, or leave an unsent order for the entry loop.

    Returns True when the caller should stop this cycle (mystery position,
    or an empty snapshot that must not be trusted).
    """
    pos = st.get("position")
    pending = st.get("pending_open")
    if pending and pending.get("episode_id") in (st.get("acted") or {}):
        print(f"  clearing pending {pending.get('episode_id')}; already acted")
        st["pending_open"] = None
        pending = None
    if pos and pending and pending.get("episode_id") == pos.get("episode_id"):
        st["pending_open"] = None
        pending = None
    if (pos is None and pending and pending.get("order_sent")
            and live_pos is None and equity <= 0.01):
        print("  pending order but implausible empty snapshot — skipping cycle")
        if now - st.get("last_api_fail_alert", 0) > 6 * 3600:
            tg("⚠️ Divergence bot: empty snapshot while an order is pending. "
               "Not sending another order, in case the read is a glitch.")
            st["last_api_fail_alert"] = now
        save_state(st)
        return True
    if pos is None and pending:
        res = pending_resolution(pending, live_pos, now, PENDING_MAX_WAIT_SEC)
        print(f"  pending {pending.get('episode_id')} order_sent={pending.get('order_sent')} → {res}")
        if res == "adopt":
            entry = live_entry_price(live_pos) or pending.get("signal_price")
            if entry:
                _finish_open(st, pending, entry, recovered=True)
            elif not pending.get("price_alerted"):
                tg(f"🚨 Position for {pending.get('episode_id')} is open but has no price yet, "
                   f"so the stop was not placed. Will retry next run.")
                pending["price_alerted"] = True
                st["pending_open"] = pending
                save_state(st)
                checkpoint()
        elif res == "give_up":
            tg(f"🚨 Order for {pending.get('episode_id')} was sent but no position appeared. "
               f"Giving up without sending a second order.")
            st.setdefault("acted", {})[pending["episode_id"]] = now
            st["pending_open"] = None
            save_state(st)
            checkpoint()
        elif res == "wait" and not pending.get("wait_alerted"):
            tg(f"⏳ Order for {pending.get('episode_id')} is waiting for the position to appear. "
               f"Not placing a second order.")
            pending["wait_alerted"] = True
            st["pending_open"] = pending
            save_state(st)
            checkpoint()
    pos = st.get("position")
    pending = st.get("pending_open")
    matches_pending = bool(pending) and live_matches(
        live_pos, pending.get("side"), pending.get("size"))
    if pos is None and live_pos is not None and not matches_pending:
        extra = ""
        if pending:
            extra = (f" A bot order for {pending.get('episode_id')} is still pending "
                     f"and will not be sent again.")
        tg(f"⚠️ Divergence bot: found a {SYMBOL} position it didn't open "
           f"(size {live_pos.get('size')}). Standing down — close it or move manual trades "
           f"off this subaccount.{extra}")
        save_state(st)
        return True
    return False

def _manage_position(st, now):
    pos = st.get("position")
    if not pos:
        return
    held_h = (now - pos["entry_ts"]) / 3600
    if held_h >= MAX_HOURS:
        close_position_now(st, "TIME_EXIT_48H")
        return
    protected = has_tpsl()
    if protected is None:
        print("  tp/sl status unknown — not closing on price this cycle")
        return
    if protected is True:
        if not pos.get("tp_sl_ok"):
            pos["tp_sl_ok"] = True
            print("  tp/sl present on exchange; recording that")
        return
    # Exchange check succeeded and there is no bracket.
    try:
        p = dec_price()
        short = pos["direction"] == "bearish"
        sl_px, tp_px = pos.get("sl_price"), pos.get("tp_price")
        breach_sl = sl_px is not None and ((p >= sl_px) if short else (p <= sl_px))
        breach_tp = tp_px is not None and ((p <= tp_px) if short else (p >= tp_px))
        if breach_sl:
            close_position_now(st, "SL_SOFTWARE")
        elif breach_tp:
            close_position_now(st, "TP_SOFTWARE")
        else:
            ok, tp, sl = place_protection(pos["direction"], pos["entry_price"])
            pos["tp_price"], pos["sl_price"] = tp, sl
            pos["tp_sl_ok"] = ok
            if ok:
                tg(f"✅ Divergence bot: TP/SL now in place (TP {_px(tp)} / SL {_px(sl)})")
            elif now - pos.get("tpsl_alert_ts", 0) > 3600:
                tg("🚨 Divergence bot: stop and target are still not confirmed on the exchange. "
                   "Will keep trying. A price exit runs only when the check succeeds and shows no orders.")
                pos["tpsl_alert_ts"] = now
    except Exception as e:
        print(f"  fallback management failed: {e}")
        if now - pos.get("tpsl_alert_ts", 0) > 3600:
            tg(f"🚨 Divergence bot: could not check or replace the stop/target ({e}). Will retry next run.")
            pos["tpsl_alert_ts"] = now

def _act_on_signals(st, eps, equity, live_pos, now):
    """Enter signals that are still fresh enough, or record a one-time skip."""
    if st.get("pending_open") and st["pending_open"].get("order_sent"):
        print("  an order is already in flight — not placing another entry")
        return
    handled = set(st.get("acted") or {}) | set(st.get("skip_noted") or {})
    tp_frac = TP_USD / POSITION_USD
    sl_frac = SL_USD / POSITION_USD
    needs_mark = False
    for ep in eps:
        probe = ep.get("confirm_price") or 1
        if classify_entry(ep, now, probe, handled, TRADE_TF, BLOCK_DAYS,
                          tp_frac, sl_frac, MAX_ENTRY_AGE_SEC) == "enter":
            needs_mark = True
            break
    unsent = st.get("pending_open") and not st["pending_open"].get("order_sent")
    mark = None
    price_ok = True
    if needs_mark or unsent:
        price_ok = False
        try:
            mark = dec_price()
            price_ok = True
        except Exception as e:
            print(f"  dec_price failed: {e}")
    counts = {}
    candidates = []
    deferred_for_price = False
    for ep in eps:
        decision = classify_entry(
            ep, now, mark if price_ok else None, handled, TRADE_TF, BLOCK_DAYS,
            tp_frac, sl_frac, MAX_ENTRY_AGE_SEC)
        counts[decision] = counts.get(decision, 0) + 1
        if decision == "enter":
            candidates.append(ep)
        elif (decision == "missing_price" and not price_ok
              and ep.get("confirm_price") not in (None, "")):
            deferred_for_price = True
        elif decision in SKIP_TEXT:
            note_skip(st, ep, decision, now)
    if counts:
        print(f"  signal decisions: {counts}")
    if deferred_for_price and now - st.get("last_price_alert", 0) > 3600:
        tg("🚨 Could not read the BTC price, so new entries are paused this run. Will try again next run.")
        st["last_price_alert"] = now
    candidates.sort(key=lambda e: e["confirmed_ts"])
    for ep in candidates:
        action = plan_order(st, ep["id"], live_pos, now, PENDING_MAX_WAIT_SEC)
        if action in ("wait", "blocked_pending", "give_up"):
            print(f"  {ep['id']} not sent ({action})")
            break
        if action != "send":
            continue
        pos = st.get("position")
        if pos is None:
            open_position(st, ep, equity)
            if st.get("position"):
                continue
            pending_now = st.get("pending_open") or {}
            # In-flight or rejected order: do not try another signal this run.
            # A margin / size / dry-run skip stamps acted and leaves no pending,
            # so a later signal in this same run can still be considered.
            if pending_now.get("order_sent") or pending_now.get("episode_id") == ep["id"]:
                break
            continue
        elif ep["direction"] == pos["direction"]:
            st["acted"][ep["id"]] = now
            log_event({"action": "SKIP_SAME_DIRECTION", "episode_id": ep["id"]})
            tg(f"↔️ Divergence bot: new {ep['direction']} signal ({ep['id']}) while already "
               f"{pos['direction']} — skipped (same direction).")
        else:
            tg(f"🔁 Divergence bot: opposing {ep['direction']} signal ({ep['id']}) — flipping.")
            if close_position_now(st, "FLIP_ON_OPPOSITE"):
                live_now, equity, api_ok = live_snapshot()
                if api_ok and live_now is None:
                    open_position(st, ep, equity)
                elif api_ok:
                    tg("🚨 Close reported success but a position is still open — not flipping yet.")
            break
    pending = st.get("pending_open")
    if pending and not pending.get("order_sent") and str(pending.get("episode_id", "")).startswith("MANUAL-"):
        open_position(st, {"id": pending["episode_id"], "direction": pending["direction"],
                           "confirm_price": pending.get("confirm_price")},
                      equity, size_usd=pending.get("size_usd") or MANUAL_SIZE_USD)
        pending = st.get("pending_open")
    if pending and not pending.get("order_sent"):
        pid = pending.get("episode_id")
        if pid not in {e["id"] for e in candidates}:
            print(f"  dropping unsent pending {pid}; signal is no longer enterable")
            st["pending_open"] = None

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    now = int(time.time())
    print(f"divergence_trader[{ACCOUNT}] {datetime.now(timezone.utc).isoformat()} "
          f"enabled={ENABLED} dry_run={DRY_RUN} trade_tf={sorted(TRADE_TF)} "
          f"block_days={sorted(BLOCK_DAYS) or 'none'} "
          f"entry_window_h={ {k: v // 3600 for k, v in MAX_ENTRY_AGE_SEC.items()} } "
          f"state={os.path.basename(STATE_FILE)}")
    if not (DEC_PRIVATE_KEY and DEC_SUB and DEC_NODE_KEY):
        print(f"  [{ACCOUNT}] Decibel credentials missing — no-op."); return

    install_cli()

    # equity graph samples are recorded whenever creds work, even with trading off
    live_pos, equity, api_ok = live_snapshot()
    if api_ok:
        # 2026-10-15/16 incident: a sustained Decibel cred/API failure returned
        # successful-but-empty snapshots for ~8h straight (9 cycles) — api_ok
        # stayed True (no exception, just zero balance), so this wrote $0.00
        # into the equity graph every cycle even though implausible() was
        # already catching the identical pattern down in the trading path.
        # Reuse that same signature here: a known-open position + no live
        # position + ~$0 equity is never a trustworthy equity sample.
        peeked_pos = load_state().get("position")
        if implausible(bool(peeked_pos), live_pos, equity):
            print(f"  [{ACCOUNT}] record_equity: skipping — implausible empty snapshot "
                  f"(state open, no live position, equity ${equity:.2f})")
        else:
            record_equity(equity)

    if MANUAL_SIDE in ("long", "short"):
        if not api_ok:
            print(f"  [{ACCOUNT}] MANUAL TRADE requested but API unavailable — aborting."); return
        manual_trade(load_state(), equity)
        return

    if not ENABLED:
        print("  DIV_TRADING_ENABLED != true — equity sampled, no trading."); return

    st = load_state()
    eps = load_episodes()

    if not api_ok:
        # loud but rate-limited: silent credential death is how the old bot
        # sat unnoticed for 17 days
        if now - st.get("last_api_fail_alert", 0) > 6 * 3600:
            tg("🚨 Divergence bot: Decibel API/credentials FAILING while trading is enabled — "
               "no trades possible. Check DECIBEL_PRIVATE_KEY delegation on Decibel + the "
               "GitHub secret. (next reminder in 6h)")
            st["last_api_fail_alert"] = now
        print("  API unavailable — skipping all position management this cycle.")
        save_state(st); return

    pos = st.get("position")

    # ── reconcile a position we already track ──
    if pos and live_pos is None:
        age = now - pos["entry_ts"]
        if implausible(True, live_pos, equity):
            tg("⚠️ Divergence bot: implausible empty snapshot (state open, no position, ~$0 equity) — skipping cycle.")
            save_state(st); return
        if age > GRACE_SEC:
            # TP or SL fired server-side (or manual/liquidation close)
            pnl = None if pos.get("entry_equity") is None else equity - pos["entry_equity"]
            cancel_all_tpsl()
            log_event({"action": "CLOSE", "episode_id": pos["episode_id"],
                       "reason": "TP_SL_FILLED", "direction": pos["direction"],
                       "entry_price": pos["entry_price"], "pnl": pnl,
                       "held_hours": round(age / 3600, 1)})
            ptxt = "?" if pnl is None else f"{pnl:+.2f}"
            tg(f"🎯 <b>Divergence bot: position closed on-exchange</b> (TP/SL/other)\n"
               f"{pos['direction']} from {pos['entry_price']:,.0f}, held {age/3600:.1f}h · "
               f"equity-based PnL ${ptxt}")
            st["position"] = None
            save_state(st)
            checkpoint()

    if _resolve_pending(st, live_pos, equity, now):
        return

    # ── manage open position (including one just recovered) ──
    _manage_position(st, now)

    # ── act on signals, including ones a previous run found late ──
    _act_on_signals(st, eps, equity, live_pos, now)

    # acted is only the recent memory of "we touched this". skip_noted has to
    # outlive it, or a pruned id would be alerted again on the next run.
    st["acted"] = {k: v for k, v in st.get("acted", {}).items() if now - v < 7 * 86400}
    st["skip_noted"] = {k: v for k, v in st.get("skip_noted", {}).items()
                        if isinstance(v, (int, float)) and now - v < 30 * 86400}
    save_state(st)
    checkpoint()
    print(f"  done. position={'open' if st.get('position') else 'none'} "
          f"pending={'yes' if st.get('pending_open') else 'no'}")

if __name__ == "__main__":
    main()
