#!/usr/bin/env python3
"""
Automated WLFI trading on Hyperliquid — fully isolated from every other
system in this project (own state/trade/equity files, own DRY_RUN gate),
but shares the same Hyperliquid account/credentials as hyperliquid_trader.py
(PUMP) since both trade on one account, just different coins.

Signal source differs from the PUMP trader: there is no hourly WLFI watchlist
feed. Divergence 100 (divergence100_update.py) refreshes WLFI's 2h candle
cache once/day. Rather than add WLFI to that pipeline's output schema, this
script re-derives the live episode list itself each run, straight from the
same cached candles (data/divergence100/WLFIUSDT.json) via
divergence100_lib.build_episode_stats() — pure computation on data already on
disk, no extra network calls, always sees same-day-fresh candles when run as
the next step after divergence100_update.py in divergence100.yml.

Parameters below come from a dedicated TP/SL/hold-time backtest over WLFI's
full ~360-day, 39-episode history (see conversation 2026-10-01): TP=8% in
both directions (the one robustly-populated optimum — wider TPs looked
better only because 1-2 outlier trades happened to hit them), SL=7% in both
directions (user chose protection-first: this tightens bearish for free — it
was already the single best bearish config at this level — and costs some
bullish upside by occasionally cutting a trade that would have recovered, a
known and accepted trade-off, see conversation for the alternate "SL=7%
bear/15% bull" return-maximizing option if this needs revisiting).

Design mirrors hyperliquid_trader.py exactly otherwise:
  - 3x ISOLATED leverage, $200 notional per trade (matches the backtest)
  - Entry + TP + SL placed atomically via bulk_orders(grouping="normalTpsl")
  - 48h max-hold backstop (matches the backtest's forward-tracking window)
  - Stands down on any WLFI position it doesn't recognize
  - WLFI_HL_DRY_RUN (default true): all mutating exchange calls are replaced
    with a printed description; read-only calls still run for real.

Env: HYPERLIQUID_API_PRIVATE_KEY, HYPERLIQUID_ACCOUNT_ADDRESS (shared with
the PUMP trader), WLFI_HL_DRY_RUN ("true"/"false"), TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID.
"""
import json, os, time
from datetime import datetime, timezone

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import divergence100_lib as d100
from divergence_monitor import tg

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(HERE, "wlfi_trader_state.json")
TRADES_FILE = os.path.join(HERE, "wlfi_trades.json")
EQUITY_FILE = os.path.join(HERE, "wlfi_equity.json")

SYMBOL          = "WLFI"
BINANCE_SYMBOL  = "WLFIUSDT"      # candle cache key — see divergence100_lib
LEVERAGE        = 3
IS_CROSS        = False           # isolated margin
NOTIONAL_USD    = 200
TP_PCT          = 8               # same for both directions — see module docstring
SL_PCT          = 7               # same for both directions — see module docstring
MAX_HOLD_HOURS  = 48              # matches the backtest's forward-tracking window
SLIPPAGE        = 0.05            # entry IOC slippage bound
TRIGGER_SLIP    = 0.02            # limit_px cushion beyond the TP/SL trigger price
# WLFI signals only refresh once/day (divergence100_update.py); 26h gives a
# small buffer past that 24h cadence rather than PUMP's 3h (hourly feed).
FRESH_SIGNAL_SEC = 26 * 3600

DRY_RUN = os.environ.get("WLFI_HL_DRY_RUN", "true").strip().lower() != "false"
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
        # see hyperliquid_trader.py's identical method for why both paths are checked
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


def fresh_wlfi_signal(state):
    """Re-derive WLFI's episode list fresh from the cached daily candles
    (read-only, no network) rather than reading a pre-built signal file —
    see module docstring for why."""
    candles = d100.load_candles(BINANCE_SYMBOL)
    if len(candles) < 60:
        print(f"  WARNING: only {len(candles)} WLFI candles cached — too few to detect divergences")
        return None
    eps, _ = d100.build_episode_stats(candles)
    now = int(time.time())
    acted = set(state.get("acted", []))
    candidates = [e for e in eps if e["id"] not in acted
                  and now - e["confirmed_ts"] <= FRESH_SIGNAL_SEC]
    return candidates[-1] if candidates else None


def main():
    print(f"wlfi_trader {datetime.now(timezone.utc).isoformat()} "
          f"dry_run={DRY_RUN} leverage={LEVERAGE}x notional=${NOTIONAL_USD} "
          f"tp={TP_PCT}% sl={SL_PCT}%")
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

    state["last_price"] = mid
    bot_pos = state.get("position")

    if bot_pos and not live_pos:
        pnl = realized_pnl_since(trader, bot_pos["opened_ts"])
        held_h = (now - bot_pos["opened_ts"]) / 3600
        reason = "TP/SL (see pnl sign)" if pnl is not None else "unknown"
        trade = {"side": bot_pos["side"], "entry_px": bot_pos["entry_px"], "sz": bot_pos["sz"],
                 "opened_ts": bot_pos["opened_ts"], "closed_ts": now, "held_hours": round(held_h, 1),
                 "pnl_usd": pnl, "reason": reason}
        trades.append(trade)
        save(TRADES_FILE, trades)
        state["position"] = None
        tg(f"{'✅' if (pnl or 0) >= 0 else '🛑'} <b>WLFI position closed</b>\n"
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
                    tg(f"⏱ <b>WLFI position force-closed at 48h max hold</b>\nPnL: "
                       f"{'unknown' if pnl is None else f'${pnl:+.2f}'}")
        else:
            print("  WARNING: live position side doesn't match bot state — standing down")
            tg("⚠️ WLFI: live Hyperliquid position doesn't match bot's recorded state. Standing down — check manually.")

    elif not bot_pos and live_pos:
        print("  WARNING: unrecognized WLFI position on the exchange (not opened by this bot) — standing down")
        tg("⚠️ WLFI: found an open Hyperliquid position this bot didn't open. Standing down, not touching it.")

    else:
        sig = fresh_wlfi_signal(state)
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
                tg(f"{arrow} <b>WLFI position opened — {side_label}</b>\n"
                   f"Size: {sz} WLFI (~${NOTIONAL_USD} @ {LEVERAGE}x isolated)\n"
                   f"Entry ~{mid:.6g}  TP +{TP_PCT}%  SL -{SL_PCT}%\n"
                   f"Response: {resp}")
            else:
                tg(f"🧪 <b>[DRY RUN] Would open WLFI position — {side_label}</b>\n"
                   f"Size: {sz} WLFI (~${NOTIONAL_USD} @ {LEVERAGE}x isolated)\n"
                   f"Entry ~{mid:.6g}  TP +{TP_PCT}%  SL -{SL_PCT}%\n"
                   f"No real order placed — WLFI_HL_DRY_RUN is still on.")
            print(f"  {'[DRY RUN] would open' if DRY_RUN else 'OPENED'}: {sig['id']}")
        else:
            print("  no open position, no fresh signal")

    equity.append({"ts": now, "equity": account_value})
    save(EQUITY_FILE, equity)
    save(STATE_FILE, state)


if __name__ == "__main__":
    main()
