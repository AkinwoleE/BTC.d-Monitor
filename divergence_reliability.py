#!/usr/bin/env python3
"""
Entry-reliability helpers for the divergence bot.

No network and no strategy math. The monitor and the trader call these so
catch-up, retries, idempotency, and durable files can be tested on their own.

What this does NOT change: pivot rules, RSI thresholds, position size,
long/short handling, or which weekdays are blocked. Those stay in the caller.
"""
import copy
import glob
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

# 2h entries stay eligible long enough to cover the longest gap we measured
# between runs (~10h) plus buffer. 1h is unchanged, so turning 1h back on
# does not start taking stale 1h signals. Weeks-old signals stay out.
MAX_ENTRY_AGE_SEC = {"1h": 2 * 3600, "2h": 18 * 3600}

# Favorable move larger than this fraction of the take-profit is "already
# gone" — half of the +3.4% target is 1.7%. Adverse move of a full stop
# (2%) is skipped. A smaller adverse move, like the ~1.3% worse fill on
# 2026-09-23, is still entered.
FAVORABLE_TP_FRACTION = 0.5

# After an order is sent, wait this long for the position to show up before
# giving up. Giving up alerts and does not send a second order.
PENDING_MAX_WAIT_SEC = 45 * 60

# Skip alerts are for signals the bot actually missed, not the archived history.
SKIP_ALERT_AGE_SEC = 7 * 86400

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TRANSIENT_NEEDLES = (
    "timeout", "nonce", "429", "502", "503", "504",
    "connection", "reset", "no json-rpc response",
)
_MISSING = object()


class TransientError(RuntimeError):
    """An order or read failed in a way that is worth trying again."""


class DurableJsonError(RuntimeError):
    """A JSON file is corrupt and could not be restored from its backup."""


def utc_weekday(ts):
    """English weekday abbreviation from a UTC timestamp (Mon..Sun).

    Locale-independent. strftime('%a') follows the machine locale, which
    would silently disable the Sat/Sun block on a non-English runner.
    """
    return _WEEKDAYS[datetime.fromtimestamp(int(ts), tz=timezone.utc).weekday()]


def closed_since(candles, last_open_ts, now, interval_sec):
    """Closed candles whose open is strictly after last_open_ts.

    A candle is closed once open + interval_sec <= now. The still-forming
    candle is excluded. last_open_ts is the open time of the last candle a
    successful run already evaluated. None means this is the first marker:
    return nothing so a deploy does not treat the whole history as a gap.
    """
    if last_open_ts is None:
        return []
    last = int(last_open_ts)
    interval = int(interval_sec)
    now = int(now)
    missed = []
    for candle in candles:
        ts = int(candle["ts"])
        if ts > last and ts + interval <= now:
            missed.append(candle)
    return missed


def price_drift_ok(direction, confirm, current, tp_frac, sl_frac):
    """(ok, reason). reason is ok | missing_price | price_ran_ahead | price_ran_against."""
    try:
        confirm_px = float(confirm)
        current_px = float(current)
    except (TypeError, ValueError):
        return False, "missing_price"
    if confirm_px <= 0 or current_px <= 0:
        return False, "missing_price"
    move = (current_px - confirm_px) / confirm_px
    if direction == "bullish":
        favorable, adverse = move, -move
    elif direction == "bearish":
        favorable, adverse = -move, move
    else:
        return False, "missing_price"
    if favorable > FAVORABLE_TP_FRACTION * float(tp_frac):
        return False, "price_ran_ahead"
    if adverse >= float(sl_frac):
        return False, "price_ran_against"
    return True, "ok"


def classify_entry(ep, now, current_price, handled_ids, trade_tfs, block_days,
                   tp_frac, sl_frac, max_age_by_tf=None):
    """enter, or why this episode must not be entered.

    handled_ids covers both acted (we traded or permanently refused it) and
    skip_noted (we already alerted, or silently stamped an old archive row).
    """
    eid = ep.get("id")
    if eid in handled_ids:
        return "already_acted"
    if ep.get("tf") not in trade_tfs:
        return "wrong_tf"
    try:
        confirmed = int(ep["confirmed_ts"])
    except (KeyError, TypeError, ValueError):
        return "too_old"
    if utc_weekday(confirmed) in set(block_days or ()):
        return "blocked_day"
    windows = max_age_by_tf or MAX_ENTRY_AGE_SEC
    max_age = windows.get(ep.get("tf"), 2 * 3600)
    if int(now) - confirmed > max_age:
        return "too_old"
    direction = ep.get("direction")
    if direction not in ("bullish", "bearish"):
        return "bad_direction"
    ok, reason = price_drift_ok(
        direction, ep.get("confirm_price"), current_price, tp_frac, sl_frac)
    if not ok:
        return reason
    return "enter"


def should_alert_skip(confirmed_ts, now, max_age_sec=SKIP_ALERT_AGE_SEC):
    """True when a skipped signal is recent enough that a human should hear about it."""
    try:
        return int(now) - int(confirmed_ts) <= int(max_age_sec)
    except (TypeError, ValueError):
        return False


def is_transient_error(err):
    """True for timeouts, nonce clashes, rate limits, and dropped connections.

    A parameter / validation error from the exchange is permanent: retrying
    it will not place the order, and may hide a real reject.
    """
    if err is None:
        return False
    if isinstance(err, TransientError):
        return True
    response = getattr(err, "response", None)
    code = getattr(response, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = f"{type(err).__name__} {err}".lower()
    return any(needle in text for needle in _TRANSIENT_NEEDLES)


def payload_is_transient(payload):
    """True when an order BODY (not an exception) describes a transient reject."""
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload)
    except (TypeError, ValueError):
        text = str(payload)
    return is_transient_error(RuntimeError(text))


def retry_call(fn, attempts=3, base_delay=1.0, sleep=time.sleep, on_transient=None):
    """Call fn up to `attempts` times. Sleep base_delay, then 2x, between tries.

    Permanent errors are raised on the first failure. on_transient(err, index)
    may return a non-None value to stop retrying (for example the position
    showed up after a timeout). Returning None continues the backoff.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as err:
            last = err
            if not is_transient_error(err) or i == attempts - 1:
                raise
            if on_transient is not None:
                found = on_transient(err, i)
                if found is not None:
                    return found
            sleep(base_delay * (2 ** i))
    raise last


def live_side(pos):
    """'long' or 'short' from a Decibel position payload, or None."""
    if not isinstance(pos, dict):
        return None
    for key in ("side", "direction", "positionSide"):
        raw = pos.get(key)
        if isinstance(raw, str):
            label = raw.lower()
            if label in ("long", "buy", "bullish"):
                return "long"
            if label in ("short", "sell", "bearish"):
                return "short"
    if "isLong" in pos and pos.get("isLong") is not None:
        return "long" if pos.get("isLong") else "short"
    try:
        size = float(pos.get("size"))
    except (TypeError, ValueError):
        return None
    if size > 0:
        return "long"
    if size < 0:
        return "short"
    return None


def live_entry_price(pos):
    if not isinstance(pos, dict):
        return None
    for key in ("entryPrice", "avgEntryPrice", "averageEntryPrice",
                "entry_price", "avgPrice", "fillPrice"):
        raw = pos.get(key)
        if raw not in (None, ""):
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None


def live_matches(pos, side, size, tolerance=0.25):
    """True when the live position is the same side and within tolerance of size."""
    if not isinstance(pos, dict) or side not in ("long", "short"):
        return False
    if live_side(pos) != side:
        return False
    try:
        got = abs(float(pos.get("size")))
        want = abs(float(size))
    except (TypeError, ValueError):
        return False
    if want <= 0:
        return False
    return abs(got - want) / want <= float(tolerance)


def pending_resolution(pending, live_pos, now, max_wait_sec):
    """What to do with a crash-recovery record.

    none        — nothing pending
    adopt       — live position matches the order we sent; manage it
    wait        — order was sent (or a different position is open); do not send again
    give_up     — sent, nothing appeared before max_wait_sec; alert, do not resend
    retry_send  — we never got as far as sending; the entry may still be placed
    """
    if not pending:
        return "none"
    if live_matches(live_pos, pending.get("side"), pending.get("size")):
        return "adopt"
    sent = bool(pending.get("order_sent"))
    try:
        age = int(now) - int(pending.get("ts") or now)
    except (TypeError, ValueError):
        age = 0
    if sent:
        if live_pos is None and age > int(max_wait_sec):
            return "give_up"
        return "wait"
    if live_pos is None:
        return "retry_send"
    return "wait"


def plan_order(state, episode_id, live_pos, now, grace_sec):
    """skip | adopt | wait | send | blocked_pending | give_up.

    order_sent True never comes back as send. grace_sec is the pending wait.
    """
    state = state or {}
    acted = state.get("acted") or {}
    noted = state.get("skip_noted") or {}
    if episode_id in acted or episode_id in noted:
        return "skip"
    pending = state.get("pending_open")
    if not pending:
        return "send"
    if pending.get("episode_id") != episode_id:
        return "blocked_pending"
    res = pending_resolution(pending, live_pos, now, grace_sec)
    if res == "retry_send":
        return "send"
    if res in ("adopt", "wait", "give_up"):
        return res
    return "wait"


def backup_if_valid(path):
    """Copy path to path.bak only when path is valid JSON.

    A corrupt file must not replace the last good backup.
    """
    path = str(path)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            json.load(handle)
    except Exception:
        return False
    bak = path + ".bak"
    tmp = bak + ".tmp"
    shutil.copyfile(path, tmp)
    os.replace(tmp, bak)
    return True


def atomic_write_json(path, obj, indent=1):
    """Write JSON via temp file + fsync + replace. Refresh .bak first if valid."""
    path = str(path)
    backup_if_valid(path)
    raw = json.dumps(obj, indent=indent)
    if not raw.endswith("\n"):
        raw += "\n"
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def quarantine_copy(path):
    """Copy corrupt bytes aside. The original path is left in place."""
    path = str(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = f"{path}.corrupt-{stamp}"
    n = 0
    while os.path.exists(dest):
        n += 1
        dest = f"{path}.corrupt-{stamp}-{n}"
    shutil.copyfile(path, dest)
    return dest


def read_json_durable(path, default=_MISSING):
    """Load JSON. Missing → deepcopy(default). Corrupt → restore .bak or raise.

    On corruption the original bytes are copied to path.corrupt-<utc> and,
    when no good backup exists, the original file is NOT replaced.
    """
    path = str(path)
    if not os.path.exists(path):
        if default is _MISSING:
            raise FileNotFoundError(path)
        return copy.deepcopy(default)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as err:
        dest = quarantine_copy(path)
        bak = path + ".bak"
        restored = None
        if os.path.exists(bak):
            try:
                with open(bak, encoding="utf-8") as handle:
                    restored = json.load(handle)
            except json.JSONDecodeError:
                restored = None
        if restored is None:
            raise DurableJsonError(
                f"{path} is corrupt ({err}). No usable backup. "
                f"Original left in place; copy saved at {dest}."
            ) from err
        shutil.copyfile(bak, path)
        return restored


def flush_git_state(paths, enabled=False, alert=None, cwd=None):
    """Commit and push the given files so a crash cannot lose them.

    No-op unless enabled is true. Callers pass enabled only when
    GITHUB_ACTIONS is set, so tests and laptops never push.
    On push rejection, rebase once and try a second push. Failure alerts
    and returns False; it does not raise.
    """
    if not enabled:
        return False
    alert = alert or (lambda _msg: None)
    cwd = cwd or os.getcwd()

    def run(args):
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True)

    try:
        existing = []
        for path in paths:
            if not path:
                continue
            if os.path.exists(path):
                existing.append(path)
            bak = str(path) + ".bak"
            if os.path.exists(bak):
                existing.append(bak)
            existing.extend(glob.glob(str(path) + ".corrupt-*"))
        if not existing:
            return False
        added = run(["git", "add", "--"] + existing)
        if added.returncode != 0:
            alert(f"git add failed: {(added.stderr or added.stdout)[:300]}")
            return False
        quiet = run(["git", "diff", "--cached", "--quiet"])
        if quiet.returncode == 0:
            return True
        if quiet.returncode != 1:
            alert(f"git diff failed: {(quiet.stderr or quiet.stdout)[:300]}")
            return False
        run(["git", "config", "user.email", "actions@users.noreply.github.com"])
        run(["git", "config", "user.name", "divergence-bot"])
        committed = run(
            ["git", "commit", "-m", "divergence: checkpoint state [skip ci]"])
        if committed.returncode != 0:
            alert(f"git commit failed: {(committed.stderr or committed.stdout)[:300]}")
            return False
        ref = os.environ.get("GITHUB_REF_NAME") or "main"
        pushed = run(["git", "push", "origin", f"HEAD:{ref}"])
        if pushed.returncode != 0:
            run(["git", "pull", "--rebase", "--autostash", "origin", ref])
            pushed = run(["git", "push", "origin", f"HEAD:{ref}"])
            if pushed.returncode != 0:
                alert(f"git push failed: {(pushed.stderr or pushed.stdout)[:300]}")
                return False
        return True
    except Exception as err:
        alert(f"checkpoint error: {err}")
        return False
