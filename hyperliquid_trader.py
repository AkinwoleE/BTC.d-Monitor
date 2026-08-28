#!/usr/bin/env python3
"""
Automated PUMP trading on Hyperliquid — fully isolated from every other
system in this project (own state/trade/equity files, own credentials, own
workflow step). Reads confirmed signals from token_alerts.json (written by
token_alerts.py) rather than re-implementing detection; never writes that
file.

Design (see conversation 2026-08-28/29 for the full reasoning):
  - 3x ISOLATED leverage, $500 notional per trade (~$166.67 margin)
  - Entry + TP(10%) + SL(15%) placed atomically via bulk_orders(grouping=
    "normalTpsl") so the exchange enforces TP/SL natively — the bot's own
    poll cadence is not a safety mechanism, just state sync + a 48h max-hold
    backstop (matches the backtest's 48h forward window; a large share of
    the validated TP10/SL15 combo's episodes were 48h time-exits, not TP/SL
    hits, so without this cap the live strategy would silently diverge from
    what was actually swept).
  - Stands down (does nothing, alerts) on any PUMP position it doesn't
    recognize — same principle as the BTC bot: never touch a manual trade.
  - DRY_RUN (default true): all mutating exchange calls are replaced with a
    printed description of what would be submitted; read-only calls (state,
    price, fills) still run for real so dry-run output reflects the actual
    account.

Env: HYPERLIQUID_API_PRIVATE_KEY (agent wallet secret_key), HYPERLIQUID_
ACCOUNT_ADDRESS (master account, NOT the agent's own address), HL_DRY_RUN
("true"/"false"), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
import json, os, time
from datetime import datetime, timezone

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from divergence_monitor import tg

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(HERE, "hyperliquid_trader_state.json")
TRADES_FILE = os.path.join(HERE, "hyperliquid_trades.json")
EQUITY_FILE = os.path.join(HERE, "hyperliquid_equity.json")
SIGNAL_FILE = os.path.join(HERE, "token_alerts.json")

SYMBOL       = "PUMP"
LEVERAGE     = 3
IS_CROSS     = False              # isolated margin
NOTIONAL_USD = 500
TP_PCT       = 10
SL_PCT       = 15
MAX_HOLD_HOURS = 48               # matches the backtest's forward-tracking window
SLIPPAGE     = 0.05               # entry IOC slippage bound
TRIGGER_SLIP = 0.02               # limit_px cushion beyond the TP/SL trigger price
FRESH_SIGNAL_SEC = 3 * 3600       # only act on signals confirmed this recently

DRY_RUN = os.environ.get("HL_DRY_RUN", "true").strip().lower() != "false"
PRIVATE_KEY = os.environ.get("HYPERLIQUID_API_PRIVATE_KEY", "")
ACCOUNT_ADDRESS = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "")


def load(path, default):
    try:
        return json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save(path, data):
    json.dump(data, open(path, "w"), indent=1)

def round_sig(px, sig=5, decimals=6):
    return round(float(f"{px:.{sig}g}"), decimals)

def slippage_px(mid, is_buy, slippage=SLIPPAGE):
    px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
    return round_sig(px)


class Trader:
    def __init__(self):
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = None
        if PRIVATE_KEY and ACCOUNT_ADDRESS:
            wallet = Account.from_key(PRIVATE_KEY)
            self.exchange = Exchange(wallet, base_url=constants.MAINNET_API_URL,
                                      account_address=ACCOUNT_ADDRESS)

    def user_state(self):
        return self.info.user_state(ACCOUNT_ADDRESS)

    def account_value(self, us):
        # Hyperliquid's newer "Unified account" mode merges spot+perps into
        # one balance; per their docs, clearinghouseState's marginSummary
        # becomes "not meaningful" for unified accounts and the real balance
        # lives in spotClearinghouseState's USDC entry instead. Standard
        # (non-unified) accounts report a real value directly in
        # marginSummary, so check both and use whichever is populated.
        cross_val = float(us.get("marginSummary", {}).get("accountValue", 0))
        if cross_val > 0:
            return cross_val
        try:
            spot = self.info.spot_user_state(ACCOUNT_ADDRESS)
            usdc = next((b for b in spot.get("balances", []) if b["coin"] == "USDC"), None)
            return float(usdc["total"]) if usdc else 0.0
        except Exception as e:
            print(f"  could not fetch spot balance: {e}")
            return cross_val

    def mid_price(self):
        return float(self.info.all_mids()[SYMBOL])

    def live_position(self, state):
        for ap in state.get("assetPositions", []):
            p = ap["position"]
            if p["coin"] == SYMBOL and float(p["szi"]) != 0:
                return p
        return None

    def open_orders(self):
        return self.info.open_orders(ACCOUNT_ADDRESS)

    # ---- mutating actions (no-op'd under DRY_RUN) ----

    def ensure_leverage(self):
        if DRY_RUN:
            print(f"  [DRY RUN] would set {SYMBOL} leverage={LEVERAGE}x isolated")
            return
        self.exchange.update_leverage(LEVERAGE, SYMBOL, is_cross=IS_CROSS)

    def open_position(self, is_buy, sz, mid):
        entry_limit = slippage_px(mid, is_buy)
        tp_trigger = mid * (1 + TP_PCT / 100) if is_buy else mid * (1 - TP_PCT / 100)
        sl_trigger = mid * (1 - SL_PCT / 100) if is_buy else mid * (1 + SL_PCT / 100)
        tp_trigger, sl_trigger = round_sig(tp_trigger), round_sig(sl_trigger)
        exit_is_buy = not is_buy
        # limit_px cushion: BUY (closing a short) needs a ceiling above the
        # trigger to still fill on a fast move; SELL (closing a long) needs a
        # floor below it. slippage_px()'s is_buy convention already does this.
        tp_limit = slippage_px(tp_trigger, exit_is_buy, TRIGGER_SLIP)
        sl_limit = slippage_px(sl_trigger, exit_is_buy, TRIGGER_SLIP)

        orders = [
            {"coin": SYMBOL, "is_buy": is_buy, "sz": sz, "limit_px": entry_limit,
             "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": False},
            {"coin": SYMBOL, "is_buy": exit_is_buy, "sz": sz, "limit_px": tp_limit,
             "order_type": {"trigger": {"isMarket": True, "triggerPx": tp_trigger, "tpsl": "tp"}},
             "reduce_only": True},
            {"coin": SYMBOL, "is_buy": exit_is_buy, "sz": sz, "limit_px": sl_limit,
             "order_type": {"trigger": {"isMarket": True, "triggerPx": sl_trigger, "tpsl": "sl"}},
             "reduce_only": True},
        ]
        print(f"  entry={'LONG' if is_buy else 'SHORT'} sz={sz} @~{mid} "
              f"tp_trigger={tp_trigger} sl_trigger={sl_trigger}")
        if DRY_RUN:
            print(f"  [DRY RUN] would submit bulk_orders(grouping=normalTpsl): {json.dumps(orders, indent=2)}")
            return {"dry_run": True}
        return self.exchange.bulk_orders(orders, grouping="normalTpsl")

    def force_close(self, reason):
        print(f"  closing {SYMBOL} position (reason={reason})")
        if DRY_RUN:
            print(f"  [DRY RUN] would cancel resting {SYMBOL} orders and market_close()")
            return
        for o in self.open_orders():
            if o.get("coin") == SYMBOL:
                self.exchange.cancel(SYMBOL, o["oid"])
        self.exchange.market_close(SYMBOL)


def realized_pnl_since(trader, since_ts):
    try:
        fills = trader.info.user_fills(ACCOUNT_ADDRESS)
    except Exception as e:
        print(f"  could not fetch fills: {e}")
        return None
    total = 0.0
    found = False
    for f in fills:
        if f.get("coin") != SYMBOL:
            continue
        if f.get("time", 0) / 1000 < since_ts:
            continue
        pnl = f.get("closedPnl")
        if pnl is not None:
            total += float(pnl)
            found = True
    return total if found else None


def fresh_pump_signal(state):
    data = load(SIGNAL_FILE, {})
    eps = data.get(SYMBOL, {}).get("episodes", [])
    now = int(time.time())
    acted = set(state.get("acted", []))
    candidates = [e for e in eps if e["id"] not in acted
                  and now - e["confirmed_ts"] <= FRESH_SIGNAL_SEC]
    return candidates[-1] if candidates else None


def main():
    print(f"hyperliquid_trader {datetime.now(timezone.utc).isoformat()} "
          f"dry_run={DRY_RUN} leverage={LEVERAGE}x notional=${NOTIONAL_USD}")
    if not PRIVATE_KEY or not ACCOUNT_ADDRESS:
        print("  HYPERLIQUID_API_PRIVATE_KEY / HYPERLIQUID_ACCOUNT_ADDRESS not set — read-only checks only")

    trader = Trader()
    state = load(STATE_FILE, {"position": None, "acted": []})
    trades = load(TRADES_FILE, [])
    equity = load(EQUITY_FILE, [])

    us = trader.user_state()
    account_value = trader.account_value(us)
    live_pos = trader.live_position(us)
    mid = trader.mid_price()
    now = int(time.time())
    print(f"  account_value=${account_value:.2f}  {SYMBOL} mid={mid}  "
          f"live_position={'none' if not live_pos else live_pos['szi']}")

    bot_pos = state.get("position")

    if bot_pos and not live_pos:
        # position we opened is now flat -> closed via TP, SL, or liquidation
        pnl = realized_pnl_since(trader, bot_pos["opened_ts"])
        held_h = (now - bot_pos["opened_ts"]) / 3600
        reason = "TP/SL (see pnl sign)" if pnl is not None else "unknown"
        trade = {"side": bot_pos["side"], "entry_px": bot_pos["entry_px"], "sz": bot_pos["sz"],
                 "opened_ts": bot_pos["opened_ts"], "closed_ts": now, "held_hours": round(held_h, 1),
                 "pnl_usd": pnl, "reason": reason}
        trades.append(trade)
        save(TRADES_FILE, trades)
        state["position"] = None
        tg(f"{'✅' if (pnl or 0) >= 0 else '🛑'} <b>PUMP position closed</b>\n"
           f"{bot_pos['side'].upper()} {bot_pos['sz']} @ {bot_pos['entry_px']:.6g} → held {held_h:.1f}h\n"
           f"Realized PnL: {'unknown' if pnl is None else f'${pnl:+.2f}'}")
        print(f"  CLOSED: {trade}")

    elif bot_pos and live_pos:
        if bot_pos["side"] == ("long" if float(live_pos["szi"]) > 0 else "short"):
            held_h = (now - bot_pos["opened_ts"]) / 3600
            print(f"  position still open, held {held_h:.1f}h")
            if held_h >= MAX_HOLD_HOURS:
                trader.force_close("max-hold-48h")
                if not DRY_RUN:
                    pnl = realized_pnl_since(trader, bot_pos["opened_ts"])
                    trades.append({"side": bot_pos["side"], "entry_px": bot_pos["entry_px"],
                                   "sz": bot_pos["sz"], "opened_ts": bot_pos["opened_ts"],
                                   "closed_ts": now, "held_hours": round(held_h, 1),
                                   "pnl_usd": pnl, "reason": "TIME"})
                    save(TRADES_FILE, trades)
                    state["position"] = None
                    tg(f"⏱ <b>PUMP position force-closed at 48h max hold</b>\nPnL: "
                       f"{'unknown' if pnl is None else f'${pnl:+.2f}'}")
        else:
            print("  WARNING: live position side doesn't match bot state — standing down")
            tg("⚠️ PUMP: live Hyperliquid position doesn't match bot's recorded state. Standing down — check manually.")

    elif not bot_pos and live_pos:
        print("  WARNING: unrecognized PUMP position on the exchange (not opened by this bot) — standing down")
        tg("⚠️ PUMP: found an open Hyperliquid position this bot didn't open. Standing down, not touching it.")

    else:
        sig = fresh_pump_signal(state)
        if sig:
            is_buy = sig["direction"] == "bullish"
            sz = round(NOTIONAL_USD / mid)
            trader.ensure_leverage()
            resp = trader.open_position(is_buy, sz, mid)
            state.setdefault("acted", []).append(sig["id"])
            side_label = "LONG" if is_buy else "SHORT"
            arrow = "🔼" if is_buy else "🔻"
            if not DRY_RUN:
                state["position"] = {"side": "long" if is_buy else "short", "entry_px": mid,
                                      "sz": sz, "opened_ts": now, "signal_id": sig["id"]}
                tg(f"{arrow} <b>PUMP position opened — {side_label}</b>\n"
                   f"Size: {sz} PUMP (~${NOTIONAL_USD} @ {LEVERAGE}x isolated)\n"
                   f"Entry ~{mid:.6g}  TP +{TP_PCT}%  SL -{SL_PCT}%\n"
                   f"Response: {resp}")
            else:
                tg(f"🧪 <b>[DRY RUN] Would open PUMP position — {side_label}</b>\n"
                   f"Size: {sz} PUMP (~${NOTIONAL_USD} @ {LEVERAGE}x isolated)\n"
                   f"Entry ~{mid:.6g}  TP +{TP_PCT}%  SL -{SL_PCT}%\n"
                   f"No real order placed — HL_DRY_RUN is still on.")
            print(f"  {'[DRY RUN] would open' if DRY_RUN else 'OPENED'}: {sig['id']}")
        else:
            print("  no open position, no fresh signal")

    equity.append({"ts": now, "equity": account_value})
    save(EQUITY_FILE, equity)
    save(STATE_FILE, state)


if __name__ == "__main__":
    main()
