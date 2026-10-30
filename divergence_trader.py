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
    frac_tp, frac_sl = TP_USD / POSITION_USD, SL_USD / POSITION_USD
    if direction == "bullish":   # long
        tp, sl = entry * (1 + frac_tp), entry * (1 - frac_sl)
        sl_lim = sl * 0.995      # limit through the trigger so the stop fills
    else:                        # short
        tp, sl = entry * (1 - frac_tp), entry * (1 + frac_sl)
        sl_lim = sl * 1.005
    return round(tp, 1), round(sl, 1), round(sl_lim, 1)

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
    size_usd = POSITION_USD if size_usd is None else size_usd
    direction = ep["direction"]
    side = "long" if direction == "bullish" else "short"
    need = size_usd / LEVERAGE * 1.15
    if equity < need:
        tg(f"⚠️ Divergence trade SKIPPED — insufficient margin: equity ${equity:.2f} &lt; ${need:.2f} "
           f"needed for ${size_usd:.0f} @ {LEVERAGE}x. Deposit to enable.")
        st["acted"][ep["id"]] = int(time.time())
        return
    price = dec_price()
    size = round(size_usd / price, 5)
    info = market_info()
    if info and size < info["min_size"]:
        tg(f"⚠️ Divergence trade SKIPPED — size {size} below market min {info['min_size']}")
        st["acted"][ep["id"]] = int(time.time())
        return
    if DRY_RUN:
        tg(f"🧪 DRY RUN — would OPEN {side.upper()} {SYMBOL} sz={size} @ ~{price:,.0f} on {ep['id']}")
        st["acted"][ep["id"]] = int(time.time())
        return
    try:
        run_cli("set_leverage", {"symbol": SYMBOL, "leverage": LEVERAGE, "marginType": "cross"})
    except Exception as e:
        print(f"  set_leverage warning: {e}")
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
    tpsl_ok, tp, sl = False, None, None
    try:
        tpsl_ok, tp, sl = place_protection(direction, entry)
    except Exception as e:
        print(f"  place_tp_sl raised: {e}")
    st["position"] = {"episode_id": ep["id"], "direction": direction, "size": size,
                      "entry_price": entry, "entry_ts": int(time.time()),
                      "entry_equity": equity, "tp_price": tp, "sl_price": sl,
                      "tp_sl_ok": tpsl_ok}
    st["acted"][ep["id"]] = int(time.time())
    log_event({"action": "OPEN", "episode_id": ep["id"], "direction": direction,
               "side": side, "size": size, "entry_price": entry, "equity": equity,
               "tp_price": tp, "sl_price": sl, "tp_sl_ok": tpsl_ok})
    arrow = "🔼" if side == "long" else "🔻"
    warn = "" if tpsl_ok else "\n🚨 TP/SL placement FAILED — will retry every run + software fallback active."
    tg(f"{arrow} <b>Divergence bot OPENED {side.upper()} {SYMBOL}</b>\n"
       f"Signal: {ep['id']}\nSize: {size} (${size_usd:.0f} @ {LEVERAGE}x)\n"
       f"Entry ~{entry:,.0f} · TP {tp:,.0f} · SL {sl:,.0f} · time-exit {MAX_HOURS:.0f}h{warn}")

def close_position_now(st, reason):
    pos = st["position"]
    if DRY_RUN:
        tg(f"🧪 DRY RUN — would CLOSE ({reason})")
        st["position"] = None
        return True
    cancel_all_tpsl()
    r = run_cli("close_position", {"symbol": SYMBOL, "slippage": SLIPPAGE})
    if not order_ok(r):
        tg(f"🚨 Divergence CLOSE FAILED ({reason}): {json.dumps(r)[:200]} — will retry next run.")
        return False
    time.sleep(2)
    _, equity, ok = live_snapshot()
    pnl = (equity - pos["entry_equity"]) if ok else None
    log_event({"action": "CLOSE", "episode_id": pos["episode_id"], "reason": reason,
               "direction": pos["direction"], "entry_price": pos["entry_price"],
               "pnl": pnl, "held_hours": round((time.time() - pos["entry_ts"]) / 3600, 1)})
    ptxt = f"{pnl:+.2f}" if pnl is not None else "?"
    tg(f"✅ <b>Divergence bot CLOSED</b> ({reason})\n"
       f"{pos['direction']} from {pos['entry_price']:,.0f}, held "
       f"{(time.time()-pos['entry_ts'])/3600:.1f}h · equity-based PnL ${ptxt}")
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
    print(f"divergence_trader[{ACCOUNT}] {datetime.now(timezone.utc).isoformat()} "
          f"enabled={ENABLED} dry_run={DRY_RUN} trade_tf={sorted(TRADE_TF)} "
          f"block_days={sorted(BLOCK_DAYS) or 'none'} state={os.path.basename(STATE_FILE)}")
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
    try:
        eps = json.load(open(DIV_FILE)).get("episodes", [])
    except Exception as e:
        print(f"  cannot read divergences.json ({e}) — skipping cycle."); return

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
            log_event({"action": "CLOSE", "episode_id": pos["episode_id"],
                       "reason": "TP_SL_FILLED", "direction": pos["direction"],
                       "entry_price": pos["entry_price"], "pnl": pnl,
                       "held_hours": round(age / 3600, 1)})
            tg(f"🎯 <b>Divergence bot: position closed on-exchange</b> (TP/SL/other)\n"
               f"{pos['direction']} from {pos['entry_price']:,.0f}, held {age/3600:.1f}h · "
               f"equity-based PnL ${pnl:+.2f}")
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

    # ── act on fresh signals ──
    fresh = [e for e in eps
             if e["tf"] in TRADE_TF
             and datetime.fromtimestamp(e["confirmed_ts"], tz=timezone.utc).strftime("%a") not in BLOCK_DAYS
             and now - e["confirmed_ts"] <= FRESH.get(e["tf"], 7200)
             and e["id"] not in st["acted"]]
    fresh.sort(key=lambda e: e["confirmed_ts"])
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

    # prune acted entries older than 7d
    st["acted"] = {k: v for k, v in st["acted"].items() if now - v < 7 * 86400}
    save_state(st)
    print(f"  done. position={'open' if st.get('position') else 'none'}")

if __name__ == "__main__":
    main()
