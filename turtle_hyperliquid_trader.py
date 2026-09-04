#!/usr/bin/env python3
"""
Live multi-asset Turtle System 2 trading on Hyperliquid across the 18-token
roster (see turtle_multiasset_fetch.py). System 2 only (55-day entry /
20-day exit, no skip filter) - System 1 was unprofitable at every risk
fraction tested on this correlated crypto basket, per conversation.
Signals computed from Binance daily data (turtle_hyperliquid_lib.py);
execution on Hyperliquid.

Design differences from hyperliquid_trader.py's single-market PUMP bot
(see conversation for the full gap analysis - this is not a simple port):

  - Exits are DYNAMIC (20-day rolling channel), not a fixed TP/SL set once
    at entry, so they can't be "set and forget" the way PUMP's native
    trigger orders were. Only the 2N stop stays fixed between pyramid
    adds, so that's placed as a native resting stop order (protection
    even if a daily run is missed); the channel exit and pyramid/entry
    decisions are evaluated once daily against the newly-closed candle
    and executed as market orders.
  - risk_capital_fraction=0.25: only 25% of total account equity is ever
    used as the position-sizing/leverage basis; the rest sits untouched.
    Backtested sweet spot for System 2 was 25-50%; chose the conservative
    end given this trades real money across 18 markets at once.
  - Portfolio-wide 3x leverage cap enforced on TOTAL notional across all
    18 markets combined (not per-market) - matches what "3x leverage"
    means for one account and matches how the backtest was run.
  - Stands down on any market with an open position it doesn't recognize,
    exactly like the PUMP bot, but checked across all 18 markets.

Run once daily, after each UTC daily candle closes.

Env: HYPERLIQUID_API_PRIVATE_KEY, HYPERLIQUID_ACCOUNT_ADDRESS, HL_DRY_RUN
("true"/"false"), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
import json, os, time
from datetime import datetime, timezone

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from divergence_monitor import tg
from turtle_backtest import compute_N, hh, ll, RISK_PCT, DOLLARS_PER_POINT, MAX_UNITS
from turtle_hyperliquid_lib import TOKENS, update_all

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(HERE, "turtle_hyperliquid_state.json")
TRADES_FILE = os.path.join(HERE, "turtle_hyperliquid_trades.json")
EQUITY_FILE = os.path.join(HERE, "turtle_hyperliquid_equity.json")

ENTRY_N, EXIT_N = 55, 20          # System 2 only - see docstring
RISK_CAPITAL_FRACTION = 0.25
LEVERAGE_CAP = 3
LEVERAGE_SETTING = 3              # per-market leverage set on Hyperliquid
SLIPPAGE = 0.05
TRIGGER_SLIP = 0.02

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

def stop_price(units):
    latest = units[-1]
    n = latest["n_at_entry"]
    return latest["entry_px"] - 2 * n if latest["side"] == "long" else latest["entry_px"] + 2 * n


def with_retry(fn, retries=3, delay=2):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  transient error ({e}), retrying in {delay}s...")
            time.sleep(delay)


class Trader:
    def __init__(self):
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = None
        if PRIVATE_KEY and ACCOUNT_ADDRESS:
            wallet = Account.from_key(PRIVATE_KEY)
            self.exchange = Exchange(wallet, base_url=constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)

    def user_state(self):
        return with_retry(lambda: self.info.user_state(ACCOUNT_ADDRESS))

    def account_value(self, us):
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

    def live_positions(self, us):
        return {ap["position"]["coin"]: ap["position"] for ap in us.get("assetPositions", [])
                if float(ap["position"]["szi"]) != 0}

    def realized_pnl_since(self, tok, since_ts):
        try:
            fills = self.info.user_fills(ACCOUNT_ADDRESS)
        except Exception as e:
            print(f"  could not fetch fills for {tok}: {e}")
            return None
        total, found = 0.0, False
        for f in fills:
            if f.get("coin") != tok or f.get("time", 0) / 1000 < since_ts:
                continue
            pnl = f.get("closedPnl")
            if pnl is not None:
                total += float(pnl)
                found = True
        return total if found else None

    def ensure_leverage(self, tok):
        if DRY_RUN:
            print(f"  [DRY RUN] would set {tok} leverage={LEVERAGE_SETTING}x isolated")
            return
        self.exchange.update_leverage(LEVERAGE_SETTING, tok, is_cross=False)

    def open_market(self, tok, is_buy, sz):
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} market {'BUY' if is_buy else 'SELL'} {sz:.6g} {tok}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.market_open(tok, is_buy, sz)

    def close_market(self, tok):
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} market_close {tok}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.market_close(tok)

    def cancel_all(self, tok):
        if DRY_RUN:
            print(f"  [DRY RUN] would cancel resting orders for {tok}")
            return
        for o in self.info.open_orders(ACCOUNT_ADDRESS):
            if o.get("coin") == tok:
                self.exchange.cancel(tok, o["oid"])

    def place_stop(self, tok, side, sz, stop_px):
        """Native resting stop protecting the position between daily runs."""
        exit_is_buy = side != "long"
        limit_px = slippage_px(stop_px, exit_is_buy, TRIGGER_SLIP)
        order = {"coin": tok, "is_buy": exit_is_buy, "sz": sz, "limit_px": limit_px,
                 "order_type": {"trigger": {"isMarket": True, "triggerPx": round_sig(stop_px), "tpsl": "sl"}},
                 "reduce_only": True}
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} place stop for {tok}: trigger={round_sig(stop_px)}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.bulk_orders([order], grouping="na")


def portfolio_notional(state, mids):
    total = 0.0
    for tok, st in state["positions"].items():
        units = st.get("units", [])
        if not units:
            continue
        px = float(mids.get(tok, units[-1]["entry_px"]))
        total += sum(u["sz"] for u in units) * px
    return total


def main():
    now = int(time.time())
    print(f"turtle_hyperliquid_trader {datetime.now(timezone.utc).isoformat()} dry_run={DRY_RUN} "
          f"risk_capital_fraction={RISK_CAPITAL_FRACTION} leverage_cap={LEVERAGE_CAP}x")
    if not PRIVATE_KEY or not ACCOUNT_ADDRESS:
        print("  HYPERLIQUID_API_PRIVATE_KEY / HYPERLIQUID_ACCOUNT_ADDRESS not set — read-only checks only")

    trader = Trader()
    state = load(STATE_FILE, {"positions": {}})
    for tok in TOKENS:
        state["positions"].setdefault(tok, {"units": []})
    trades = load(TRADES_FILE, [])
    equity_log = load(EQUITY_FILE, [])

    print("Updating daily candles from Binance...")
    candle_data = update_all(now_ts=now)
    N_data = {tok: compute_N(candle_data[tok]) for tok in TOKENS}

    us = trader.user_state()
    account_value = trader.account_value(us)
    live_pos = trader.live_positions(us)
    mids = with_retry(lambda: trader.info.all_mids())
    risk_capital = RISK_CAPITAL_FRACTION * account_value
    print(f"account_value=${account_value:.2f}  risk_capital=${risk_capital:.2f}  "
          f"open live positions: {sorted(live_pos.keys())}")

    for tok in TOKENS:
        candles, N = candle_data[tok], N_data[tok]
        if len(candles) < ENTRY_N + 5:
            print(f"  {tok}: not enough history yet, skipping")
            continue
        i = len(candles) - 1
        c, n = candles[i], N[i]
        if n is None or n <= 0:
            continue

        st = state["positions"][tok]
        bot_has_pos = bool(st["units"])
        live_has_pos = tok in live_pos

        if bot_has_pos and not live_has_pos:
            side = st["units"][0]["side"]
            pnl = trader.realized_pnl_since(tok, st["units"][0]["entry_ts"])
            held_h = (now - st["units"][0]["entry_ts"]) / 3600
            trades.append({"token": tok, "side": side, "units": len(st["units"]),
                           "entry_ts": st["units"][0]["entry_ts"], "exit_ts": now,
                           "held_hours": round(held_h, 1), "pnl": pnl, "reason": "CLOSED_EXTERNALLY"})
            tg(f"Turtle/{tok}: position no longer showing on Hyperliquid (closed via stop or externally). "
               f"PnL: {'unknown' if pnl is None else f'${pnl:+.2f}'}")
            st["units"] = []
            continue

        if not bot_has_pos and live_has_pos:
            print(f"  {tok}: WARNING unrecognized live position — standing down")
            tg(f"⚠️ Turtle/{tok}: found an open Hyperliquid position this bot didn't open. Standing down.")
            continue

        if bot_has_pos and live_has_pos:
            side_expected = st["units"][0]["side"]
            side_actual = "long" if float(live_pos[tok]["szi"]) > 0 else "short"
            if side_expected != side_actual:
                print(f"  {tok}: WARNING side mismatch — standing down")
                tg(f"⚠️ Turtle/{tok}: live position side doesn't match bot state. Standing down.")
                continue

        if bot_has_pos:
            side = st["units"][0]["side"]
            stop = stop_price(st["units"])
            stopped = (side == "long" and c["low"] <= stop) or (side == "short" and c["high"] >= stop)
            if side == "long":
                exit_lvl = ll(candles, i, EXIT_N); channel_hit = c["low"] <= exit_lvl
            else:
                exit_lvl = hh(candles, i, EXIT_N); channel_hit = c["high"] >= exit_lvl

            if stopped or channel_hit:
                trader.cancel_all(tok)
                trader.close_market(tok)
                exit_px = stop if stopped else exit_lvl
                pnl = sum((exit_px - u["entry_px"]) * u["sz"] if side == "long"
                          else (u["entry_px"] - exit_px) * u["sz"] for u in st["units"])
                trades.append({"token": tok, "side": side, "units": len(st["units"]),
                               "entry_ts": st["units"][0]["entry_ts"], "exit_ts": now,
                               "pnl": round(pnl, 2), "reason": "STOP" if stopped else "CHANNEL_EXIT"})
                tg(f"{'🛑' if stopped else '✅'} <b>Turtle/{tok} closed</b> ({'STOP' if stopped else 'CHANNEL_EXIT'}): "
                   f"pnl ${pnl:+.2f}")
                st["units"] = []
            elif len(st["units"]) < MAX_UNITS:
                last_fill = st["units"][-1]["entry_px"]
                trigger = last_fill + 0.5 * n if side == "long" else last_fill - 0.5 * n
                add = (side == "long" and c["high"] >= trigger) or (side == "short" and c["low"] <= trigger)
                if add:
                    sz = (RISK_PCT * risk_capital) / (n * DOLLARS_PER_POINT)
                    prospective = portfolio_notional(state, mids) + sz * trigger
                    if prospective <= LEVERAGE_CAP * risk_capital:
                        trader.open_market(tok, side == "long", sz)
                        st["units"].append({"side": side, "entry_px": trigger, "sz": sz,
                                            "n_at_entry": n, "entry_ts": now})
                        trader.cancel_all(tok)
                        trader.place_stop(tok, side, sum(u["sz"] for u in st["units"]), stop_price(st["units"]))
                        tg(f"Turtle/{tok}: pyramid unit {len(st['units'])} added @ ~{trigger:.6g}")
                    else:
                        print(f"  {tok}: pyramid triggered but portfolio leverage cap would be breached, skipping")
        else:
            hi_entry, lo_entry = hh(candles, i, ENTRY_N), ll(candles, i, ENTRY_N)
            long_sig, short_sig = c["high"] > hi_entry, c["low"] < lo_entry
            if long_sig or short_sig:
                side = "long" if long_sig else "short"
                entry_px = hi_entry if side == "long" else lo_entry
                sz = (RISK_PCT * risk_capital) / (n * DOLLARS_PER_POINT)
                prospective = portfolio_notional(state, mids) + sz * entry_px
                if prospective <= LEVERAGE_CAP * risk_capital:
                    trader.ensure_leverage(tok)
                    trader.open_market(tok, side == "long", sz)
                    st["units"] = [{"side": side, "entry_px": entry_px, "sz": sz, "n_at_entry": n, "entry_ts": now}]
                    stop = stop_price(st["units"])
                    trader.place_stop(tok, side, sz, stop)
                    tg(f"{'🔼' if side == 'long' else '🔻'} <b>Turtle/{tok} opened {side.upper()}</b>\n"
                       f"Size: {sz:.6g} @ ~{entry_px:.6g}  Stop: {stop:.6g}")
                    print(f"  {tok}: {'[DRY RUN] would open' if DRY_RUN else 'OPENED'} {side}")
                else:
                    print(f"  {tok}: signal fired but portfolio leverage cap would be breached, skipping")

    equity_log.append({"ts": now, "equity": account_value})
    save(EQUITY_FILE, equity_log)
    save(TRADES_FILE, trades)
    save(STATE_FILE, state)
    print("Done.")


if __name__ == "__main__":
    main()
