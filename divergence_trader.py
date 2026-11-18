#!/usr/bin/env python3
"""
Divergence trader — executes the backtested rule set on Decibel.

Runs as a workflow step AFTER divergence_monitor.py. Consumes divergences.json;
never detects patterns itself.

Rule set (derived from the 13-trade 2-pt backfill, 2026-07-29):
  entry      market order at signal confirmation. Notional is
             equity × DIV_RISK_PCT / stop-distance (default risk 2.5%, stop
             2% → notional = 1.25 × equity). DIV_POSITION_USD, when the
             variable is actually set, forces that fixed notional instead
             and the risk percent is ignored.
  take-profit +3.4% (default $34 per $1000), native exchange TP order.
             DIV_TP_USD / DIV_SL_USD still override the PERCENT
             (usd / DIV_POSITION_USD, or / 1000 when position is unset);
             the dollar distance scales with the computed notional so the
             price levels stay the backtest percents.
  stop-loss   −2.0% (default $20 per $1000), native exchange SL order
  time-exit   close after 48h if neither TP nor SL fired
  overlap     same-direction signal while open: skip; opposite: close & flip

Observability (does not change the rules): Telegram if this process has not
run for over an hour, or if an entry is more than 30 minutes after the signal
bar closed. Each fill logs signal time/price, entry delay, fill, slippage,
and the fee get_trade_history reports. A flat account whose equity jumps by
$1 or more with no open position is alerted (the 2026-10-06 primary-account
drop was this shape).

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

move_sl_breakeven.yml (workflow_dispatch only) can move the OPEN position's
stop to its entry without opening, closing, or resizing. It stamps
sl_breakeven on that position so the hourly repair re-places entry rather
than the 2% stop if the native order disappears. New entries never set the
flag, and it goes away when the position is cleared.

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
never silently resets; that failure mode wiped trader history four times in
the old bot).
Log:   divergence_trades{_account}.json (append-only events).

See docs/DIVERGENCE.md for the rules, the 2026-11-18 changelog, and which
repository variables override risk sizing.
"""
import os, sys, json, time, subprocess, requests
from datetime import datetime, timezone

BASE    = os.path.dirname(os.path.abspath(__file__))
ACCOUNT = (os.environ.get("TRADER_ACCOUNT", "") or "primary").strip()
SUFFIX  = "" if ACCOUNT.lower() == "primary" else f"_{ACCOUNT.lower()}"
TAG     = f"[{ACCOUNT}] "

DIV_FILE   = os.path.join(BASE, "divergences.json")               # shared signal source — never suffixed
STATE_FILE = os.path.join(BASE, f"divergence_trader_state{SUFFIX}.json")
LOG_FILE   = os.path.join(BASE, f"divergence_trades{SUFFIX}.json")
EQ_FILE    = os.path.join(BASE, f"divergence_equity{SUFFIX}.json")

# 2026-11-18: size from equity instead of a fixed $1,000. At 20x a 2% stop on
# $1,000 is ~$20, about 15% of a ~$141 account, which is how single stops
# dominated the live loss. Dollar TP/SL scale with the computed notional so
# the price percents stay +3.4% / −2%. A set DIV_POSITION_USD still forces
# that fixed notional (see docs/DIVERGENCE.md).
_DEFAULT_POSITION_USD = 1000.0
_DEFAULT_TP_USD = 34.0          # +3.4%
_DEFAULT_SL_USD = 20.0          # −2.0%
_DEFAULT_RISK_PCT = 2.5         # percent of equity lost if the stop fills
# 2026-11-18: alert when the fill is >30 min after the signal bar closed.
# GitHub/cron lag of 1–3h was about $15 worse than the backtest's bar-open fill.
LATE_ENTRY_SEC = 30 * 60
# 2026-11-18: alert when the previous saved run is more than an hour behind.
RUN_GAP_SEC = 60 * 60
# 2026-11-18: alert on a flat-account equity jump of at least $1. Primary
# equity fell 228.35 → 207.12 on 6 Oct 15:00–20:00 UTC with no position.
UNEXPLAINED_EQUITY_USD = 1.0

def env_num(name, default, cast=float):
    """GitHub Actions injects '' for undefined ${{ vars.X }} — treat as unset."""
    v = os.environ.get(name, "").strip()
    return cast(v) if v else cast(default)

def env_set(name):
    return os.environ.get(name, "").strip() != ""

def price_fractions():
    """(take-profit fraction, stop fraction) of the entry price.

    Unset variables reproduce +3.4% / −2%. A set DIV_TP_USD, DIV_SL_USD, or
    DIV_POSITION_USD changes the ratio the same way the old fixed-dollar
    formula did: fraction = usd / position, and an unset position is 1000.
    """
    pos = env_num("DIV_POSITION_USD", _DEFAULT_POSITION_USD)
    tp = env_num("DIV_TP_USD", _DEFAULT_TP_USD)
    sl = env_num("DIV_SL_USD", _DEFAULT_SL_USD)
    if pos <= 0 or tp <= 0 or sl <= 0:
        return _DEFAULT_TP_USD / _DEFAULT_POSITION_USD, _DEFAULT_SL_USD / _DEFAULT_POSITION_USD
    return tp / pos, sl / pos

def risk_pct():
    """Percent of equity the stop is allowed to lose. Non-positive falls back to 2.5."""
    pct = env_num("DIV_RISK_PCT", _DEFAULT_RISK_PCT)
    return pct if pct > 0 else _DEFAULT_RISK_PCT

def order_notional(equity, size_usd=None):
    """(notional USD, mode).

    explicit — caller passed a size (manual trade); risk sizing is not applied.
    fixed    — DIV_POSITION_USD is set, so that notional wins over DIV_RISK_PCT.
    risk     — equity × risk% / stop-distance%. A 2.5% risk and a 2% stop is
               1.25× equity.
    """
    if size_usd is not None:
        return float(size_usd), "explicit"
    if env_set("DIV_POSITION_USD"):
        return float(env_num("DIV_POSITION_USD", _DEFAULT_POSITION_USD)), "fixed"
    _, frac_sl = price_fractions()
    eq = float(equity or 0)
    if eq <= 0 or frac_sl <= 0:
        return 0.0, "risk"
    return eq * (risk_pct() / 100.0) / frac_sl, "risk"

ENABLED      = os.environ.get("DIV_TRADING_ENABLED", "").lower() == "true"
DRY_RUN      = os.environ.get("DIV_DRY_RUN", "").lower() == "true"
MAX_HOURS    = env_num("DIV_MAX_HOURS", 48)
LEVERAGE     = env_num("DIV_LEVERAGE", 20, int)
SLIPPAGE     = env_num("SLIPPAGE", 0.5)
# one-off manual test trade (workflow_dispatch only — never set on the hourly
# cron) — bypasses signal detection entirely, places a single real order
# using the normal open_position() path so it's picked up and managed
# (TP/SL/48h-exit) by every subsequent run exactly like a signal-driven trade
MANUAL_SIDE     = os.environ.get("MANUAL_SIDE", "").strip().lower()   # "long" or "short"
MANUAL_SIZE_USD = env_num("MANUAL_SIZE_USD", 0)
# one-off: move the open position's stop to its entry (workflow_dispatch
# only — never set on the hourly cron). Does not open, close, resize, or
# change leverage. Stamps sl_breakeven on the position so a later hourly
# re-place (the branch that calls tp_sl_prices when protection is missing)
# keeps the entry stop instead of restoring the default 2% stop.
MOVE_SL_TO_BREAKEVEN = os.environ.get("MOVE_SL_TO_BREAKEVEN", "").lower() == "true"
SYMBOL       = "BTC/USD"
TRADE_TF     = {t.strip() for t in os.environ.get("DIV_TRADE_TF", "1h,2h").split(",") if t.strip()}
BLOCK_DAYS   = {d.strip()[:3].title() for d in os.environ.get("DIV_BLOCK_DAYS", "").split(",") if d.strip()}
FRESH        = {"1h": 2 * 3600, "2h": 4 * 3600}   # same freshness gate as alerts
GRACE_SEC    = 600                                # reconciliation grace after entry

DEC_PRIVATE_KEY = os.environ.get("DECIBEL_PRIVATE_KEY", "")
DEC_SUB         = os.environ.get("DECIBEL_SUBACCOUNT", "")
DEC_NODE_KEY    = os.environ.get("DECIBEL_NODE_API_KEY", "")
DEC_GAS_KEY     = os.environ.get("DECIBEL_GAS_STATION_API_KEY", "")
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")

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
                raise RuntimeError(f"MCP error: {msg['error']}")
            for item in msg.get("result", {}).get("content", []):
                if item.get("type") == "text":
                    try: return json.loads(item["text"])
                    except Exception: return {"result": item["text"]}
            return msg.get("result", {})
    raise RuntimeError(f"No JSON-RPC response for {action}: {result.stdout[:200]} / {result.stderr[:200]}")

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
    r = run_cli("get_price", {"symbol": SYMBOL})
    for k in ("price", "markPrice", "lastPrice", "indexPrice"):
        if r.get(k) is not None:
            return float(r[k])
    raise RuntimeError(f"no price in {r}")

def _as_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _pick(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) is not None and d.get(k) != "":
            return d[k]
    return None

def tpsl_order_ids(snapshot):
    """Order ids from get_tp_sl: fixed-size orders plus position-level ids.

    Position-level TP/SL lives on the position object (tp_order_id /
    sl_order_id). Those ids are not always repeated in `orders`, and a cancel
    that only walks `orders` leaves the old stop in place."""
    if not isinstance(snapshot, dict):
        return []
    ids = []
    for o in snapshot.get("orders", snapshot.get("tpSlOrders", [])) or []:
        if isinstance(o, dict):
            oid = o.get("orderId") or o.get("id")
            if oid:
                ids.append(str(oid))
    p = snapshot.get("position")
    if isinstance(p, dict):
        for k in ("tpOrderId", "slOrderId", "tp_order_id", "sl_order_id"):
            if p.get(k):
                ids.append(str(p[k]))
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out

def tpsl_trigger_prices(snapshot, kind):
    """Trigger prices ('tp' or 'sl') from the position object and any orders."""
    if not isinstance(snapshot, dict):
        return []
    pkeys = (("tpTriggerPrice", "tp_trigger_price") if kind == "tp"
             else ("slTriggerPrice", "sl_trigger_price"))
    vals = []
    v = _as_float(_pick(snapshot.get("position"), *pkeys))
    if v is not None:
        vals.append(v)
    for o in snapshot.get("orders", snapshot.get("tpSlOrders", [])) or []:
        v = _as_float(_pick(o, *pkeys))
        if v is not None and v not in vals:
            vals.append(v)
    return vals

def cancel_all_tpsl():
    try:
        r = run_cli("get_tp_sl", {"symbol": SYMBOL})
        for oid in tpsl_order_ids(r):
            try:
                run_cli("cancel_tp_sl", {"symbol": SYMBOL, "orderId": oid})
                print(f"  cancelled tp/sl {oid}")
            except Exception as e:
                print(f"  cancel tp/sl {oid}: {e}")
    except Exception as e:
        print(f"  cancel_all_tpsl: {e}")

def has_tpsl():
    try:
        r = run_cli("get_tp_sl", {"symbol": SYMBOL})
        return bool(r.get("orders", r.get("tpSlOrders", [])) or [])
    except Exception:
        return False

# ── state / log ───────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except FileNotFoundError:
        return {"position": None, "acted": {}}
    except json.JSONDecodeError as e:
        tg(f"🚨 divergence_trader: STATE FILE CORRUPT ({e}) — refusing to run. Fix {STATE_FILE} manually.")
        raise

def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1)

def record_equity(equity):
    """Hourly (or on >0.5% move) equity snapshots for the dashboard's graph."""
    try:
        try:
            hist = json.load(open(EQ_FILE))
        except (FileNotFoundError, json.JSONDecodeError):
            hist = []
        now = int(time.time())
        if hist:
            last = hist[-1]
            moved = abs(equity - last["equity"]) >= max(0.005 * max(last["equity"], 1.0), 0.01)
            if now - last["ts"] < 3300 and not moved:
                return
        hist.append({"ts": now, "equity": round(equity, 2)})
        json.dump(hist[-5000:], open(EQ_FILE, "w"))
    except Exception as e:
        print(f"  record_equity: {e}")

def log_event(ev):
    try:
        log = json.load(open(LOG_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        log = []
    ev["timestamp"] = datetime.now(timezone.utc).isoformat()
    log.append(ev)
    json.dump(log, open(LOG_FILE, "w"), indent=1)

# ── trade actions ─────────────────────────────────────────────────────────────
def tp_sl_prices(direction, entry):
    frac_tp, frac_sl = price_fractions()
    if direction == "bullish":   # long
        tp, sl = entry * (1 + frac_tp), entry * (1 - frac_sl)
        sl_lim = sl * 0.995      # limit through the trigger so the stop fills
    else:                        # short
        tp, sl = entry * (1 - frac_tp), entry * (1 + frac_sl)
        sl_lim = sl * 1.005
    return round(tp, 1), round(sl, 1), round(sl_lim, 1)

def adverse_slippage(side, reference, fill):
    """(price points, percent), positive when the fill is worse than `reference`.

    A long that pays above the reference and a short that sells below it are
    both positive. Returns (None, None) when either price is missing.
    """
    try:
        reference = float(reference)
        fill = float(fill)
    except (TypeError, ValueError):
        return None, None
    if reference <= 0:
        return None, None
    sign = 1.0 if side == "long" else -1.0
    per = sign * (fill - reference)
    return round(per, 4), round(100.0 * per / reference, 4)

def iter_trade_rows(payload):
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("trades", "items", "fills", "history"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        rows = rows or []
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]

def _fill_kind(row):
    """('long'|'short', 'open'|'close') from a get_trade_history row, or (None, None)."""
    action = str(row.get("action") or "").replace("_", "").replace("-", "").lower()
    kinds = {
        "openlong": ("long", "open"), "openshort": ("short", "open"),
        "closelong": ("long", "close"), "closeshort": ("short", "close"),
    }
    if action in kinds:
        return kinds[action]
    side = str(row.get("side") or "").lower()
    if side in ("buy", "long"):
        return "long", "open"
    if side in ("sell", "short"):
        return "short", "open"
    return None, None

def _row_time_ms(row):
    raw = row.get("transaction_unix_ms", row.get("timestamp", row.get("ts")))
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts < 10 ** 12:          # seconds
        ts *= 1000.0
    return ts

def summarize_fills(payload, side, kind, since_ms=None, order_id=None):
    """Fees and VWAP for fills matching this order.

    When `order_id` is known, a row with a different id is ignored. Rows with
    no id are kept only if their timestamp is not clearly before `since_ms`.
    Returns None when nothing matched.
    """
    matched = []
    want_id = str(order_id) if order_id else ""
    for row in iter_trade_rows(payload):
        row_side, row_kind = _fill_kind(row)
        if row_side != side or row_kind != kind:
            continue
        oid = str(row.get("order_id") or row.get("orderId") or "")
        if want_id and oid and oid != want_id:
            continue
        if want_id and not oid:
            ts = _row_time_ms(row)
            if since_ms and ts is not None and ts + 5000 < since_ms:
                continue
        elif since_ms:
            ts = _row_time_ms(row)
            if ts is not None and ts + 5000 < since_ms and not (want_id and oid == want_id):
                continue
        matched.append(row)
    if not matched:
        return None
    fees = 0.0
    saw_fee = False
    notion = 0.0
    qty = 0.0
    for row in matched:
        raw_fee = row.get("fee_amount", row.get("fee", row.get("feeAmount")))
        if raw_fee is not None:
            try:
                fees += float(raw_fee)
                saw_fee = True
            except (TypeError, ValueError):
                pass
        try:
            px = float(row["price"])
            q = abs(float(row["size"]))
            notion += px * q
            qty += q
        except (TypeError, ValueError, KeyError):
            pass
    return {
        "fees": round(fees, 6) if saw_fee else None,
        "fill_price": (notion / qty) if qty else None,
        "n": len(matched),
    }

def dig_order_id(result):
    if not isinstance(result, dict):
        return None
    for key in ("orderId", "order_id", "id"):
        if result.get(key):
            return str(result[key])
    for key in ("order", "data", "result"):
        found = dig_order_id(result.get(key))
        if found:
            return found
    return None

def lookup_fills(side, kind, since_ms, order_id=None):
    """Best-effort get_trade_history lookup. Never raises; None if no fill is visible yet."""
    if DRY_RUN:
        return None
    for attempt in range(2):
        if attempt:
            time.sleep(2)
        try:
            payload = run_cli("get_trade_history", {"limit": 50})
        except Exception as e:
            print(f"  get_trade_history failed: {e}")
            continue
        summary = summarize_fills(payload, side, kind, since_ms, order_id)
        if summary:
            return summary
    print("  fees unavailable: no matching fill in get_trade_history")
    return None

def entry_audit(ep, side, decision, fill, fees, notional, mode, now=None):
    """Fields stored on the OPEN event: signal, delay, fill, slippage, fees.

    2026-11-18: live logs had none of these, so a late fill and the exchange
    fee could not be compared with the backtest. Fees come from
    get_trade_history (fee_amount); slippage is adverse vs the pre-order price.
    """
    now = int(time.time()) if now is None else int(now)
    delay = None
    if ep.get("confirmed_ts"):
        delay = now - int(ep["confirmed_ts"])
    _, slip_pct = adverse_slippage(side, decision, fill)
    _, slip_sig = adverse_slippage(side, ep.get("confirm_price"), fill)
    return {
        "signal_ts": ep.get("confirmed_ts"),
        "signal_price": ep.get("confirm_price"),
        "entry_delay_sec": delay,
        "decision_price": decision,
        "fill_price": fill,
        "slippage_pct": slip_pct,
        "slippage_vs_signal_pct": slip_sig,
        "fees": fees,
        "notional_usd": None if notional is None else round(float(notional), 2),
        "sizing": mode,
        "risk_pct": risk_pct() if mode == "risk" else None,
    }

def note_run_gap(st, now):
    """Alert when the previous saved run is more than an hour behind. Stamps now."""
    prev = st.get("last_run_ts")
    st["last_run_ts"] = int(now)
    if not prev:
        return None
    gap = int(now) - int(prev)
    if gap <= RUN_GAP_SEC:
        return None
    when = datetime.fromtimestamp(int(prev), timezone.utc).strftime("%Y-%m-%d %H:%M")
    return (f"⚠️ Divergence bot has not run for {gap / 3600:.1f}h "
            f"(last run {when} UTC). The schedule is every 15 min — "
            "check the GitHub Actions workflow.")

def observe_flat_equity(st, equity, suppress):
    """Alert when equity moves by >= $1 while no position is open.

    `suppress` is true when a position is open or this cycle just opened or
    closed one — that change is the trade, and the flat baseline is cleared
    so the next flat sample starts clean (no false alert on the trade's PnL).
    A ~$0 read is the known empty-snapshot glitch and is ignored.
    Returns the alert text, or None.
    """
    if suppress:
        st.pop("flat_equity", None)
        return None
    try:
        equity = float(equity)
    except (TypeError, ValueError):
        return None
    if equity <= 0.01:
        return None
    prev = st.get("flat_equity")
    st["flat_equity"] = round(equity, 2)
    if prev is None:
        return None
    delta = equity - float(prev)
    if abs(delta) < UNEXPLAINED_EQUITY_USD:
        return None
    return (f"⚠️ Unexplained equity change with no open position: "
            f"${float(prev):.2f} → ${equity:.2f} ({delta:+.2f}). "
            "No bot position explains this (the primary account dropped this way "
            "on 2026-10-06, 15:00–20:00 UTC).")

def actionable_signals(eps, now, acted):
    """Signals the trader may still enter. Stale, wrong-tf, blocked-day, and
    already-acted ids are left out.

    2026-11-18: this gate is unchanged. Warm-up episodes are no longer written
    down, and anything still older than FRESH (4h on 2h) is not traded.
    """
    fresh = [e for e in eps
             if e.get("tf") in TRADE_TF
             and datetime.fromtimestamp(e["confirmed_ts"], tz=timezone.utc).strftime("%a") not in BLOCK_DAYS
             and now - e["confirmed_ts"] <= FRESH.get(e.get("tf"), 7200)
             and e.get("id") not in acted]
    fresh.sort(key=lambda e: e["confirmed_ts"])
    return fresh

def protection_prices(direction, entry, tp=None, sl=None):
    """TP/SL prices to send. No overrides → exactly tp_sl_prices(), including
    the limit computed from the unrounded stop (rounding the stop first moves
    the limit by a tick). An override is a 1-decimal trigger; its limit is
    0.5% through that trigger so the stop can fill."""
    def_tp, def_sl, def_lim = tp_sl_prices(direction, entry)
    if tp is None and sl is None:
        return def_tp, def_sl, def_lim
    tp = def_tp if tp is None else round(float(tp), 1)
    sl = def_sl if sl is None else round(float(sl), 1)
    sl_lim = round(sl * (0.995 if direction == "bullish" else 1.005), 1)
    return tp, sl, sl_lim

def place_protection(direction, entry, tp=None, sl=None):
    tp, sl, sl_lim = protection_prices(direction, entry, tp=tp, sl=sl)
    r = run_cli("place_tp_sl", {"symbol": SYMBOL,
                                "tpTriggerPrice": tp, "tpLimitPrice": tp,
                                "slTriggerPrice": sl, "slLimitPrice": sl_lim})
    ok = order_ok(r)
    print(f"  tp/sl: tp={tp} sl={sl} sl_lim={sl_lim} ok={ok} {json.dumps(r)[:120]}")
    return ok, tp, sl

def position_match_problems(state_pos, live_pos, entry_tol=0.1):
    """Why the live position must not be touched. Empty list means it matches.

    Size is compared at the 5-decimal precision open_position() records.
    Entry may differ by one 0.1 tick (the precision sl/tp prices use). A
    short on Decibel is a negative size."""
    if not state_pos:
        return ["no open position in state"]
    if live_pos is None:
        return ["no live BTC position"]
    live_size = _as_float(_pick(live_pos, "size"))
    live_entry = _as_float(_pick(live_pos, "entry_price", "entryPrice"))
    if live_size is None or live_entry is None:
        keys = list(live_pos)[:12] if isinstance(live_pos, dict) else type(live_pos).__name__
        return [f"live position missing size or entry (keys={keys})"]
    problems = []
    state_size = float(state_pos["size"])
    state_entry = float(state_pos["entry_price"])
    if round(abs(live_size), 5) != round(abs(state_size), 5):
        problems.append(f"size live={live_size} state={state_size}")
    if abs(live_entry - state_entry) > entry_tol:
        problems.append(f"entry live={live_entry} state={state_entry}")
    direction = state_pos.get("direction")
    if direction == "bearish" and live_size >= 0:
        problems.append(f"state short but live size {live_size} is not negative")
    elif direction == "bullish" and live_size <= 0:
        problems.append(f"state long but live size {live_size} is not positive")
    elif direction not in ("bullish", "bearish"):
        problems.append(f"unknown state direction {direction}")
    return problems

def breakeven_replace_args(pos):
    """(tp, sl) the hourly repair sends for a position whose stop was moved
    to entry. tp None falls through to the strategy take-profit."""
    return pos.get("tp_price"), pos.get("entry_price")

def breakeven_stop_valid(direction, mark, sl):
    """Decibel rejects a trigger on the wrong side of mark (EINVALID_TRIGGER_PRICE).
    A short stop must sit strictly above mark; a long stop strictly below."""
    if mark is None or sl is None:
        return False
    if direction == "bearish":
        return mark < sl
    if direction == "bullish":
        return mark > sl
    return False

def open_position(st, ep, equity, size_usd=None):
    size_usd, mode = order_notional(equity, size_usd)
    direction = ep["direction"]
    side = "long" if direction == "bullish" else "short"
    frac_tp, frac_sl = price_fractions()
    if size_usd <= 0:
        tg(f"⚠️ Divergence trade SKIPPED — computed notional is ${size_usd:.2f} "
           f"(equity ${float(equity or 0):.2f}, sizing={mode}).")
        st["acted"][ep["id"]] = int(time.time())
        return
    need = size_usd / LEVERAGE * 1.15
    if equity < need:
        tg(f"⚠️ Divergence trade SKIPPED — insufficient margin: equity ${equity:.2f} &lt; ${need:.2f} "
           f"needed for ${size_usd:.0f} @ {LEVERAGE}x ({mode}). Deposit to enable.")
        st["acted"][ep["id"]] = int(time.time())
        return
    price = dec_price()
    size = round(size_usd / price, 5)
    info = market_info()
    if info and size < info["min_size"]:
        tg(f"⚠️ Divergence trade SKIPPED — size {size} below market min {info['min_size']} "
           f"(notional ${size_usd:.2f} at {price:,.0f}, sizing={mode}). Not rounded up.")
        st["acted"][ep["id"]] = int(time.time())
        return
    now = int(time.time())
    delay = (now - int(ep["confirmed_ts"])) if ep.get("confirmed_ts") else None
    late = delay is not None and delay > LATE_ENTRY_SEC
    tp_usd, sl_usd = size_usd * frac_tp, size_usd * frac_sl
    if DRY_RUN:
        late_txt = f" LATE by {delay / 60:.0f} min." if late else ""
        tg(f"🧪 DRY RUN — would OPEN {side.upper()} {SYMBOL} sz={size} "
           f"(${size_usd:.2f} {mode}, TP ${tp_usd:.2f} / SL ${sl_usd:.2f}) "
           f"@ ~{price:,.0f} on {ep['id']}.{late_txt}")
        st["acted"][ep["id"]] = now
        return
    try:
        run_cli("set_leverage", {"symbol": SYMBOL, "leverage": LEVERAGE, "marginType": "cross"})
    except Exception as e:
        print(f"  set_leverage warning: {e}")
    ordered_ms = int(time.time() * 1000) - 60_000
    r = run_cli("place_market_order", {"symbol": SYMBOL, "side": side, "size": size,
                                       "slippage": SLIPPAGE, "reduceOnly": False})
    if not order_ok(r):
        # do NOT stamp acted — retry next run while the signal is still fresh
        tg(f"🚨 Divergence OPEN FAILED on {ep['id']}: {json.dumps(r)[:200]}")
        return
    entry = price
    try:
        entry = float(r.get("fillPrice") or r.get("avgPrice") or price)
    except Exception:
        pass
    filled_at = int(time.time())
    tpsl_ok, tp, sl = False, None, None
    try:
        tpsl_ok, tp, sl = place_protection(direction, entry)
    except Exception as e:
        print(f"  place_tp_sl raised: {e}")
    # Protection is already on. The history lookup may sleep; it must not move
    # the TP/SL anchor, only the audit fields.
    fill_info = lookup_fills(side, "open", ordered_ms, dig_order_id(r))
    fill_price = entry
    fees = None
    if fill_info:
        if fill_info.get("fill_price"):
            fill_price = fill_info["fill_price"]
        fees = fill_info.get("fees")
    audit = entry_audit(ep, side, price, fill_price, fees, size_usd, mode, now=now)
    st["position"] = {"episode_id": ep["id"], "direction": direction, "size": size,
                      "entry_price": entry, "entry_ts": filled_at,
                      "entry_equity": equity, "tp_price": tp, "sl_price": sl,
                      "tp_sl_ok": tpsl_ok, "notional_usd": round(size_usd, 2),
                      "sizing": mode, "signal_ts": audit["signal_ts"],
                      "signal_price": audit["signal_price"],
                      "entry_delay_sec": audit["entry_delay_sec"],
                      "fees": fees}
    st["acted"][ep["id"]] = int(time.time())
    log_event({"action": "OPEN", "episode_id": ep["id"], "direction": direction,
               "side": side, "size": size, "entry_price": entry, "equity": equity,
               "tp_price": tp, "sl_price": sl, "tp_sl_ok": tpsl_ok,
               "tp_usd": round(tp_usd, 2), "sl_usd": round(sl_usd, 2), **audit})
    print(f"  OPEN audit signal_ts={audit['signal_ts']} signal_price={audit['signal_price']} "
          f"delay_sec={audit['entry_delay_sec']} decision={price} fill={fill_price} "
          f"slippage_pct={audit['slippage_pct']} fees={fees} notional={size_usd:.2f} mode={mode}")
    arrow = "🔼" if side == "long" else "🔻"
    warn = "" if tpsl_ok else "\n🚨 TP/SL placement FAILED — will retry every run + software fallback active."
    late_line = ""
    if late:
        late_line = f"\n⚠️ LATE ENTRY — signal bar closed {delay / 60:.0f} min ago (&gt;30 min)."
    fee_txt = f"${fees:.4f}" if fees is not None else "n/a"
    slip_txt = f"{audit['slippage_pct']:+.3f}%" if audit["slippage_pct"] is not None else "n/a"
    sig = audit["signal_price"]
    sig_txt = f"{sig:,.0f}" if isinstance(sig, (int, float)) else "n/a"
    risk_txt = f", risk {risk_pct():.2f}% of equity" if mode == "risk" else ""
    tg(f"{arrow} <b>Divergence bot OPENED {side.upper()} {SYMBOL}</b>\n"
       f"Signal: {ep['id']} @ {sig_txt}\n"
       f"Size: {size} (${size_usd:.2f} @ {LEVERAGE}x, {mode}{risk_txt})\n"
       f"TP ${tp_usd:.2f} ({frac_tp * 100:.2f}%) · SL ${sl_usd:.2f} ({frac_sl * 100:.2f}%)\n"
       f"Entry ~{entry:,.0f} · fill {fill_price:,.1f} · slippage {slip_txt} · fees {fee_txt}\n"
       f"TP {tp:,.0f} · SL {sl:,.0f} · time-exit {MAX_HOURS:.0f}h{late_line}{warn}")

def _exit_audit(pos, decision_price, close_result):
    """Fill price, slippage, and fees for a close. Missing history leaves the keys null."""
    side = "long" if pos.get("direction") == "bullish" else "short"
    since_ms = int(pos.get("entry_ts") or time.time()) * 1000
    info = lookup_fills(side, "close", since_ms, dig_order_id(close_result))
    fill = None
    fees = None
    if info:
        fill = info.get("fill_price")
        fees = info.get("fees")
    _, slip = adverse_slippage(side, decision_price, fill if fill is not None else decision_price)
    if fill is None or decision_price is None:
        slip = None
    return {"exit_fill_price": fill, "exit_fees": fees, "exit_slippage_pct": slip,
            "exit_decision_price": decision_price}

def close_position_now(st, reason):
    pos = st["position"]
    if DRY_RUN:
        tg(f"🧪 DRY RUN — would CLOSE ({reason})")
        st["position"] = None
        return True
    decision = None
    try:
        decision = dec_price()
    except Exception as e:
        print(f"  close decision price unavailable: {e}")
    cancel_all_tpsl()
    r = run_cli("close_position", {"symbol": SYMBOL, "slippage": SLIPPAGE})
    if not order_ok(r):
        tg(f"🚨 Divergence CLOSE FAILED ({reason}): {json.dumps(r)[:200]} — will retry next run.")
        return False
    time.sleep(2)
    _, equity, ok = live_snapshot()
    pnl = (equity - pos["entry_equity"]) if ok else None
    audit = _exit_audit(pos, decision, r)
    log_event({"action": "CLOSE", "episode_id": pos["episode_id"], "reason": reason,
               "direction": pos["direction"], "entry_price": pos["entry_price"],
               "signal_ts": pos.get("signal_ts"), "signal_price": pos.get("signal_price"),
               "entry_delay_sec": pos.get("entry_delay_sec"), "entry_fees": pos.get("fees"),
               "pnl": pnl, "held_hours": round((time.time() - pos["entry_ts"]) / 3600, 1),
               **audit})
    print(f"  CLOSE audit reason={reason} fill={audit['exit_fill_price']} "
          f"slippage_pct={audit['exit_slippage_pct']} fees={audit['exit_fees']}")
    ptxt = f"{pnl:+.2f}" if pnl is not None else "?"
    fee_txt = f"${audit['exit_fees']:.4f}" if audit["exit_fees"] is not None else "n/a"
    fill_txt = f"{audit['exit_fill_price']:,.1f}" if audit["exit_fill_price"] else "n/a"
    tg(f"✅ <b>Divergence bot CLOSED</b> ({reason})\n"
       f"{pos['direction']} from {pos['entry_price']:,.0f}, held "
       f"{(time.time()-pos['entry_ts'])/3600:.1f}h · equity-based PnL ${ptxt}\n"
       f"Exit fill {fill_txt} · fees {fee_txt}")
    st["position"] = None
    return True

def _near(values, target, tol=0.2):
    return any(abs(v - target) <= tol for v in values)

def move_sl_to_breakeven(st, live_pos, equity):
    """Move the open position's exchange SL to its entry. Keep the current TP.

    Returns a process exit code: 0 done, 2 refused before sending an order,
    1 the cancel/place did not confirm. Never opens, closes, resizes, or
    sets leverage. On a confirmed move (or a crash after cancel) the
    position is stamped sl_breakeven so the hourly path cannot restore 2%.
    """
    pos = st.get("position")
    if pos and live_pos is None and implausible(True, live_pos, equity):
        print(f"SL_MOVE result=abort account={ACCOUNT} reason=implausible_snapshot "
              f"equity={equity}")
        tg("⚠️ Stop move aborted — implausible empty snapshot "
           "(state open, no position, ~$0 equity). No orders sent.")
        return 2
    problems = position_match_problems(pos, live_pos)
    if problems:
        reason = "; ".join(problems)
        print(f"SL_MOVE result=abort account={ACCOUNT} reason={reason}")
        tg(f"⚠️ Stop move aborted — live position does not match state ({reason}). "
           f"No orders sent.")
        return 2

    state_entry = float(pos["entry_price"])
    sl = round(state_entry, 1)
    mark = _as_float(_pick(live_pos, "markPrice", "mark_price", "markPx"))
    if mark is None:
        try:
            mark = dec_price()
        except Exception as e:
            print(f"SL_MOVE result=abort account={ACCOUNT} reason=no_mark {e}")
            tg("⚠️ Stop move aborted — could not read a mark price. No orders sent.")
            return 2
    print(f"  live size={_pick(live_pos, 'size')} entry={_pick(live_pos, 'entry_price', 'entryPrice')} "
          f"mark={mark} state size={pos['size']} entry={state_entry} "
          f"dir={pos['direction']} state_sl={pos.get('sl_price')} state_tp={pos.get('tp_price')}")
    if not breakeven_stop_valid(pos["direction"], mark, sl):
        print(f"SL_MOVE result=abort account={ACCOUNT} reason=stop_not_beyond_mark "
              f"mark={mark} sl={sl} dir={pos['direction']}")
        tg(f"⚠️ Stop move aborted — SL at entry {sl:,.1f} is not beyond mark {mark:,.1f} "
           f"for a {pos['direction']} position. No orders sent.")
        return 2

    try:
        before = run_cli("get_tp_sl", {"symbol": SYMBOL})
    except Exception as e:
        print(f"SL_MOVE result=abort account={ACCOUNT} reason=get_tp_sl_failed {e}")
        tg(f"⚠️ Stop move aborted — get_tp_sl failed ({e}). No orders sent.")
        return 2
    print(f"  get_tp_sl BEFORE: {json.dumps(before)[:800]}")
    tp_vals = tpsl_trigger_prices(before, "tp")
    sl_vals = tpsl_trigger_prices(before, "sl")
    state_tp = _as_float(pos.get("tp_price"))
    tp = round(tp_vals[0], 1) if tp_vals else (round(state_tp, 1) if state_tp else None)
    if tp is None:
        print(f"SL_MOVE result=abort account={ACCOUNT} reason=no_tp_to_keep")
        tg("⚠️ Stop move aborted — no take-profit on the position or in state to keep. "
           "No orders sent.")
        return 2
    if state_tp is not None and abs(tp - state_tp) > 0.2:
        print(f"  NOTE: keeping live TP {tp} (state tp_price is {state_tp})")
    before_sl = sl_vals[0] if sl_vals else pos.get("sl_price")
    _, _, sl_lim = protection_prices(pos["direction"], state_entry, tp=tp, sl=sl)
    old_still_there = (before_sl is not None and abs(float(before_sl) - sl) > 0.2
                       and _near(sl_vals, float(before_sl)))
    already = _near(sl_vals, sl) and _near(tp_vals, tp) and not old_still_there
    print(f"  plan: keep TP {tp}  SL {before_sl} → {sl} (limit {sl_lim}) already={already}")

    if DRY_RUN:
        print(f"SL_MOVE result=dry_run account={ACCOUNT} before_sl={before_sl} "
              f"after_sl={sl} tp={tp} sl_limit={sl_lim} already={already}")
        tg(f"🧪 DRY RUN — would move SL {before_sl} → breakeven {sl:,.1f} "
           f"(limit {sl_lim:,.1f}), keep TP {tp:,.1f}."
           + (" Exchange stop is already there." if already else ""))
        return 0

    if already:
        pos["sl_breakeven"] = True
        pos["sl_price"] = sl
        pos["tp_price"] = tp
        pos["tp_sl_ok"] = True
        save_state(st)
        print(f"SL_MOVE result=ok account={ACCOUNT} before_sl={before_sl} after_sl={sl} "
              f"confirmed_sl={sl} confirmed_tp={tp} tp={tp} sl_limit={sl_lim} already=true")
        tg(f"🟦 <b>Stop already at breakeven</b>\n"
           f"{pos['direction']} {SYMBOL} · SL {sl:,.1f} · TP {tp:,.1f}\n"
           f"State stamped so later runs keep this stop.")
        return 0

    # Stamp intent before cancel. If we die after the cancel, the next hourly
    # run sees sl_breakeven and re-places entry rather than the 2% stop.
    old_sl = pos.get("sl_price")
    pos["sl_breakeven"] = True
    pos["sl_price"] = sl
    pos["tp_price"] = tp
    save_state(st)

    cancel_all_tpsl()
    try:
        ok, tp_sent, sl_sent = place_protection(pos["direction"], state_entry, tp=tp, sl=sl)
    except Exception as e:
        ok, tp_sent, sl_sent = False, tp, sl
        print(f"  place_tp_sl raised: {e}")
    if not ok:
        pos["tp_sl_ok"] = False
        save_state(st)
        log_event({"action": "MOVE_SL_BREAKEVEN", "result": "PLACE_FAILED",
                   "episode_id": pos.get("episode_id"), "direction": pos["direction"],
                   "old_sl": old_sl, "sl_price": sl, "sl_limit": sl_lim, "tp_price": tp})
        print(f"SL_MOVE result=fail account={ACCOUNT} before_sl={before_sl} "
              f"after_sl={sl} tp={tp} sl_limit={sl_lim} stage=place")
        tg(f"🚨 Stop move to breakeven FAILED after cancel. "
           f"sl_breakeven is set, so the next run re-places SL {sl:,.1f} "
           f"(not the 2% stop) and TP {tp:,.1f}.")
        return 1

    after = None
    confirmed = False
    for _ in range(4):
        time.sleep(2)
        try:
            after = run_cli("get_tp_sl", {"symbol": SYMBOL})
        except Exception as e:
            print(f"  get_tp_sl confirm failed: {e}")
            continue
        print(f"  get_tp_sl AFTER: {json.dumps(after)[:800]}")
        got_sl = tpsl_trigger_prices(after, "sl")
        got_tp = tpsl_trigger_prices(after, "tp")
        old_remains = _near(got_sl, float(before_sl)) if before_sl is not None and abs(float(before_sl) - sl) > 0.2 else False
        if _near(got_sl, sl_sent) and _near(got_tp, tp_sent) and not old_remains:
            confirmed = True
            break
    if not confirmed:
        pos["tp_sl_ok"] = False
        save_state(st)
        log_event({"action": "MOVE_SL_BREAKEVEN", "result": "UNCONFIRMED",
                   "episode_id": pos.get("episode_id"), "direction": pos["direction"],
                   "old_sl": old_sl, "sl_price": sl, "sl_limit": sl_lim, "tp_price": tp})
        print(f"SL_MOVE result=fail account={ACCOUNT} before_sl={before_sl} "
              f"after_sl={sl_sent} tp={tp_sent} sl_limit={sl_lim} stage=confirm")
        tg(f"🚨 Stop move placed but get_tp_sl did not confirm SL {sl_sent:,.1f} / TP {tp_sent:,.1f}. "
           f"Next run will cancel and re-place the breakeven stop.")
        return 1

    got_sl = tpsl_trigger_prices(after, "sl")
    got_tp = tpsl_trigger_prices(after, "tp")
    confirmed_sl = min(got_sl, key=lambda v: abs(v - sl_sent))
    confirmed_tp = min(got_tp, key=lambda v: abs(v - tp_sent))
    pos["tp_sl_ok"] = True
    pos["sl_breakeven"] = True
    pos["sl_price"] = sl_sent
    pos["tp_price"] = tp_sent
    save_state(st)
    log_event({"action": "MOVE_SL_BREAKEVEN", "result": "OK",
               "episode_id": pos.get("episode_id"), "direction": pos["direction"],
               "old_sl": old_sl, "sl_price": sl_sent, "sl_limit": sl_lim,
               "tp_price": tp_sent, "confirmed_sl": confirmed_sl,
               "confirmed_tp": confirmed_tp})
    print(f"SL_MOVE result=ok account={ACCOUNT} before_sl={before_sl} after_sl={sl_sent} "
          f"confirmed_sl={confirmed_sl} confirmed_tp={confirmed_tp} tp={tp_sent} sl_limit={sl_lim}")
    tg(f"🟦 <b>Stop moved to breakeven</b>\n"
       f"{pos['direction']} {SYMBOL}\n"
       f"SL {old_sl} → {sl_sent:,.1f} (limit {sl_lim:,.1f})\n"
       f"TP kept {tp_sent:,.1f}")
    return 0

def manual_trade(st, equity):
    """One-off test trade requested via workflow_dispatch inputs, independent
    of DIV_TRADING_ENABLED — an explicit ad-hoc action, not the strategy."""
    if st.get("position"):
        tg(f"⚠️ Manual trade SKIPPED — a position is already open on this account "
           f"({st['position']['direction']} from {st['position']['entry_price']:,.0f}). "
           f"Close it first.")
        return
    if MANUAL_SIZE_USD <= 0:
        tg("⚠️ Manual trade SKIPPED — MANUAL_SIZE_USD not set or <= 0."); return
    direction = "bullish" if MANUAL_SIDE == "long" else "bearish"
    ep = {"id": f"MANUAL-{int(time.time())}", "direction": direction}
    print(f"  [{ACCOUNT}] MANUAL TRADE: {MANUAL_SIDE} ${MANUAL_SIZE_USD:.0f} @ {LEVERAGE}x")
    open_position(st, ep, equity, size_usd=MANUAL_SIZE_USD)
    save_state(st)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    now = int(time.time())
    frac_tp, frac_sl = price_fractions()
    sizing = "fixed DIV_POSITION_USD" if env_set("DIV_POSITION_USD") else f"risk {risk_pct():.2f}%"
    print(f"divergence_trader[{ACCOUNT}] {datetime.now(timezone.utc).isoformat()} "
          f"enabled={ENABLED} dry_run={DRY_RUN} trade_tf={sorted(TRADE_TF)} "
          f"block_days={sorted(BLOCK_DAYS) or 'none'} sizing={sizing} "
          f"tp={frac_tp * 100:.2f}% sl={frac_sl * 100:.2f}% "
          f"state={os.path.basename(STATE_FILE)}")
    if not (DEC_PRIVATE_KEY and DEC_SUB and DEC_NODE_KEY):
        print(f"  [{ACCOUNT}] Decibel credentials missing — no-op."); return

    install_cli()

    if MOVE_SL_TO_BREAKEVEN:
        # Dispatch-only path. Returns before equity sampling and before any
        # signal/entry/close logic — the only write it can make is a TP/SL
        # replace on the position already in the state file.
        live_pos, equity, api_ok = live_snapshot()
        if not api_ok:
            print(f"SL_MOVE result=abort account={ACCOUNT} reason=api_unavailable")
            tg("⚠️ Stop move aborted — Decibel API unavailable. No orders sent.")
            sys.exit(2)
        sys.exit(move_sl_to_breakeven(load_state(), live_pos, equity))

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
    gap_msg = note_run_gap(st, now)
    if gap_msg:
        tg(gap_msg)
    try:
        eps = json.load(open(DIV_FILE)).get("episodes", [])
    except Exception as e:
        print(f"  cannot read divergences.json ({e}) — skipping cycle.")
        save_state(st); return

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
    had_position = bool(pos)

    # ── reconcile state vs exchange ──
    if pos and live_pos is None:
        age = now - pos["entry_ts"]
        if implausible(True, live_pos, equity):
            tg("⚠️ Divergence bot: implausible empty snapshot (state open, no position, ~$0 equity) — skipping cycle.")
            save_state(st); return
        if age > GRACE_SEC:
            # TP or SL fired server-side (or manual/liquidation close)
            pnl = equity - pos["entry_equity"]
            cancel_all_tpsl()
            audit = _exit_audit(pos, None, None)
            log_event({"action": "CLOSE", "episode_id": pos["episode_id"],
                       "reason": "TP_SL_FILLED", "direction": pos["direction"],
                       "entry_price": pos["entry_price"], "pnl": pnl,
                       "signal_ts": pos.get("signal_ts"), "signal_price": pos.get("signal_price"),
                       "entry_delay_sec": pos.get("entry_delay_sec"), "entry_fees": pos.get("fees"),
                       "held_hours": round(age / 3600, 1), **audit})
            fee_txt = f"${audit['exit_fees']:.4f}" if audit["exit_fees"] is not None else "n/a"
            fill_txt = f"{audit['exit_fill_price']:,.1f}" if audit["exit_fill_price"] else "n/a"
            tg(f"🎯 <b>Divergence bot: position closed on-exchange</b> (TP/SL/other)\n"
               f"{pos['direction']} from {pos['entry_price']:,.0f}, held {age/3600:.1f}h · "
               f"equity-based PnL ${pnl:+.2f}\n"
               f"Exit fill {fill_txt} · fees {fee_txt}")
            st["position"] = pos = None
    elif pos is None and live_pos is not None:
        tg(f"⚠️ Divergence bot: found a {SYMBOL} position it didn't open "
           f"(size {live_pos.get('size')}). Standing down — close it or move manual trades "
           f"off this subaccount.")
        save_state(st); return

    # ── manage open position ──
    if pos:
        held_h = (now - pos["entry_ts"]) / 3600
        if held_h >= MAX_HOURS:
            close_position_now(st, "TIME_EXIT_48H")
            pos = st.get("position")
        elif not pos.get("tp_sl_ok") or not has_tpsl():
            # software fallback: close on breach at this run's price, then retry native tp/sl
            try:
                p = dec_price()
                short = pos["direction"] == "bearish"
                breach_sl = (p >= pos["sl_price"]) if short else (p <= pos["sl_price"])
                breach_tp = (p <= pos["tp_price"]) if short else (p >= pos["tp_price"])
                if pos.get("sl_price") and breach_sl:
                    close_position_now(st, "SL_SOFTWARE"); pos = st.get("position")
                elif pos.get("tp_price") and breach_tp:
                    close_position_now(st, "TP_SOFTWARE"); pos = st.get("position")
                else:
                    if pos.get("sl_breakeven"):
                        # Sticky breakeven stop (2026-10-30). Cancel first so a
                        # retry cannot stack a second order, then re-place the
                        # stored entry stop — not tp_sl_prices()'s 2% stop.
                        # New trades never set sl_breakeven, so this branch is
                        # not the path a future entry takes.
                        cancel_all_tpsl()
                        tp_ov, sl_ov = breakeven_replace_args(pos)
                        ok, tp, sl = place_protection(
                            pos["direction"], pos["entry_price"], tp=tp_ov, sl=sl_ov)
                    else:
                        ok, tp, sl = place_protection(pos["direction"], pos["entry_price"])
                    if ok:
                        pos["tp_sl_ok"], pos["tp_price"], pos["sl_price"] = True, tp, sl
                        tg(f"✅ Divergence bot: TP/SL now in place (TP {tp:,.0f} / SL {sl:,.0f})")
            except Exception as e:
                print(f"  fallback management failed: {e}")

    # ── act on fresh signals (stale ones stay ignored) ──
    fresh = actionable_signals(eps, now, st["acted"])
    for ep in fresh:
        pos = st.get("position")
        if pos is None:
            open_position(st, ep, equity)
        elif ep["direction"] == pos["direction"]:
            st["acted"][ep["id"]] = now
            log_event({"action": "SKIP_SAME_DIRECTION", "episode_id": ep["id"]})
            tg(f"↔️ Divergence bot: new {ep['direction']} signal ({ep['id']}) while already "
               f"{pos['direction']} — skipped (same direction).")
        else:
            tg(f"🔁 Divergence bot: opposing {ep['direction']} signal ({ep['id']}) — flipping.")
            if close_position_now(st, "FLIP_ON_OPPOSITE"):
                _, equity, api_ok = live_snapshot()
                if api_ok:
                    open_position(st, ep, equity)
            # on close failure: acted not stamped — retried next run while fresh

    # A position, or a cycle that just closed one, explains an equity change.
    # The next flat cycle establishes a new baseline instead of alerting on the trade.
    flat_msg = observe_flat_equity(st, equity, suppress=bool(had_position or st.get("position")))
    if flat_msg:
        tg(flat_msg)

    # prune acted entries older than 7d
    st["acted"] = {k: v for k, v in st["acted"].items() if now - v < 7 * 86400}
    save_state(st)
    print(f"  done. position={'open' if st.get('position') else 'none'}")

if __name__ == "__main__":
    main()
