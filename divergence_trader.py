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

State: divergence_trader_state.json (fails LOUD on corruption — never silently
resets; that failure mode wiped trader history four times in the old bot).
Log:   divergence_trades.json (append-only events).
"""
import os, json, time, subprocess, requests
from datetime import datetime, timezone

BASE       = os.path.dirname(os.path.abspath(__file__))
DIV_FILE   = os.path.join(BASE, "divergences.json")
STATE_FILE = os.path.join(BASE, "divergence_trader_state.json")
LOG_FILE   = os.path.join(BASE, "divergence_trades.json")

ENABLED      = os.environ.get("DIV_TRADING_ENABLED", "").lower() == "true"
DRY_RUN      = os.environ.get("DIV_DRY_RUN", "").lower() == "true"
POSITION_USD = float(os.environ.get("DIV_POSITION_USD", "1000"))
TP_USD       = float(os.environ.get("DIV_TP_USD", "34"))    # per POSITION_USD
SL_USD       = float(os.environ.get("DIV_SL_USD", "20"))
MAX_HOURS    = float(os.environ.get("DIV_MAX_HOURS", "48"))
LEVERAGE     = int(os.environ.get("DIV_LEVERAGE", "20"))
SLIPPAGE     = float(os.environ.get("SLIPPAGE", "0.5"))
SYMBOL       = "BTC/USD"
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

def place_protection(direction, entry):
    tp, sl, sl_lim = tp_sl_prices(direction, entry)
    r = run_cli("place_tp_sl", {"symbol": SYMBOL,
                                "tpTriggerPrice": tp, "tpLimitPrice": tp,
                                "slTriggerPrice": sl, "slLimitPrice": sl_lim})
    ok = order_ok(r)
    print(f"  tp/sl: tp={tp} sl={sl} ok={ok} {json.dumps(r)[:120]}")
    return ok, tp, sl

def open_position(st, ep, equity):
    direction = ep["direction"]
    side = "long" if direction == "bullish" else "short"
    need = POSITION_USD / LEVERAGE * 1.15
    if equity < need:
        tg(f"⚠️ Divergence trade SKIPPED — insufficient margin: equity ${equity:.2f} &lt; ${need:.2f} "
           f"needed for ${POSITION_USD:.0f} @ {LEVERAGE}x. Deposit to enable.")
        st["acted"][ep["id"]] = int(time.time())
        return
    price = dec_price()
    size = round(POSITION_USD / price, 5)
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
       f"Signal: {ep['id']}\nSize: {size} (${POSITION_USD:.0f} @ {LEVERAGE}x)\n"
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

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    now = int(time.time())
    print(f"divergence_trader {datetime.now(timezone.utc).isoformat()} "
          f"enabled={ENABLED} dry_run={DRY_RUN}")
    if not ENABLED:
        print("  DIV_TRADING_ENABLED != true — no-op."); return
    if not (DEC_PRIVATE_KEY and DEC_SUB and DEC_NODE_KEY):
        print("  Decibel credentials missing — no-op."); return

    st = load_state()
    try:
        eps = json.load(open(DIV_FILE)).get("episodes", [])
    except Exception as e:
        print(f"  cannot read divergences.json ({e}) — skipping cycle."); return

    live_pos, equity, api_ok = live_snapshot()
    if not api_ok:
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
                    ok, tp, sl = place_protection(pos["direction"], pos["entry_price"])
                    if ok:
                        pos["tp_sl_ok"], pos["tp_price"], pos["sl_price"] = True, tp, sl
                        tg(f"✅ Divergence bot: TP/SL now in place (TP {tp:,.0f} / SL {sl:,.0f})")
            except Exception as e:
                print(f"  fallback management failed: {e}")

    # ── act on fresh signals ──
    fresh = [e for e in eps
             if now - e["confirmed_ts"] <= FRESH.get(e["tf"], 7200)
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
