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
from turtle_hyperliquid_lib import TOKENS as CRYPTO_TOKENS, update_all as crypto_update_all
from turtle_equity_lib import TOKENS as EQUITY_TOKENS, HL_SYMBOL as EQUITY_HL_SYMBOL, \
    update_all as equity_update_all

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

# ── equity/macro leg (2026-11-01) ───────────────────────────────────────────
# Added for portfolio diversification - see conversation for the full
# liquidity-screened shortlist rationale and standalone/blended backtest
# (turtle_equity_backtest.py): blended 28-asset run showed max drawdown
# 31.8%->24.2% and total return +326.6%->+354.1% vs crypto-only over the
# same window. Equities execute on Hyperliquid's "xyz" HIP-3 builder dex -
# a SEPARATE, ISOLATED-MARGIN perp dex from the main crypto perp account
# (confirmed via Hyperliquid docs: HIP-3 dexes have independent margining,
# collateral kept separate from the main perp account and from each other).
# That means: two independent account_value/risk_capital/leverage-cap pools,
# not one shared pool - a crypto signal can never compete for margin with an
# equity signal and vice versa. The user must separately fund the xyz dex
# with its own collateral on Hyperliquid before any equity order can fill;
# until then equity_account_value reads ~$0 and every equity entry hits the
# existing insufficient-margin skip in open_position(), same fail-safe as an
# underfunded crypto account - no code difference, just naturally inert.
TOKENS = CRYPTO_TOKENS + EQUITY_TOKENS
ASSET_CLASS = {tok: "crypto" for tok in CRYPTO_TOKENS}
ASSET_CLASS.update({tok: "equity" for tok in EQUITY_TOKENS})
HL_COIN = {tok: tok for tok in CRYPTO_TOKENS}
HL_COIN.update(EQUITY_HL_SYMBOL)                      # e.g. "NVDA" -> "xyz:NVDA"
REVERSE_HL_COIN = {v: k for k, v in HL_COIN.items()}  # "xyz:NVDA" -> "NVDA"

# TURTLE_EQUITY_EXCLUDE: comma-separated signal symbols to never take NEW
# entries on, same block-only-new-entries pattern as divergence_trader.py's
# DIV_BLOCK_DAYS - an existing open position in an excluded token is still
# fully managed (stop/channel-exit/pyramid). Default excludes CL: the
# standalone shortlist backtest (turtle_equity_backtest.py) showed it the
# clear outlier - 6% win rate on 17 trades, -$1,517 PnL, worst of all 28
# tokens tested including crypto - crude oil whipsawed badly against this
# rule set. Easy to re-include by setting the repo variable to "".
EQUITY_EXCLUDE = {t.strip().upper() for t in
                   os.environ.get("TURTLE_EQUITY_EXCLUDE", "CL").split(",") if t.strip()}

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

    def user_state(self, dex=""):
        return with_retry(lambda: self.info.user_state(ACCOUNT_ADDRESS, dex=dex))

    def account_value(self, us, dex=""):
        """dex="" (main perp dex) falls back to spot USDC balance when
        accountValue reads 0 - the account's spot wallet is what backs a
        first cross-margin deposit there, so that's a reasonable signal.
        A named dex (e.g. "xyz") must NOT do this: HIP-3 dexes are
        isolated-margin, a separate collateral pool from the main account
        and from spot - reporting spot balance as that dex's equity would
        make risk_capital look funded even when nothing has actually been
        allocated to it, defeating the "inert until funded" safety this
        relies on. 2026-11-01 bug found in review before this went live."""
        cross_val = float(us.get("marginSummary", {}).get("accountValue", 0))
        if cross_val > 0 or dex:
            return cross_val
        try:
            spot = self.info.spot_user_state(ACCOUNT_ADDRESS)
            usdc = next((b for b in spot.get("balances", []) if b["coin"] == "USDC"), None)
            return float(usdc["total"]) if usdc else 0.0
        except Exception as e:
            print(f"  could not fetch spot balance: {e}")
            return cross_val

    def live_positions(self, us):
        """Keyed by OUR internal signal symbol, not whatever Hyperliquid's
        coin field says. The HIP-3 dex-scoped clearinghouseState response's
        exact coin format (bare "NVDA" vs prefixed "xyz:NVDA") isn't
        documented - REVERSE_HL_COIN matches either. Unverified against a
        live xyz position at build time; first real run's printed
        'open live positions' line confirms it - see conversation."""
        out = {}
        for ap in us.get("assetPositions", []):
            pos = ap["position"]
            if float(pos["szi"]) == 0:
                continue
            coin = pos["coin"]
            bare = coin.split(":", 1)[-1]
            tok = REVERSE_HL_COIN.get(coin) or REVERSE_HL_COIN.get(f"xyz:{bare}") or \
                  (bare if bare in CRYPTO_TOKENS else None)
            if tok:
                out[tok] = pos
            else:
                print(f"  NOTE: live position on unrecognized coin '{coin}' - not in TOKENS, ignoring")
        return out

    def realized_pnl_since(self, tok, since_ts):
        coin = HL_COIN[tok]
        try:
            fills = self.info.user_fills(ACCOUNT_ADDRESS)  # account-wide, spans every dex
        except Exception as e:
            print(f"  could not fetch fills for {coin}: {e}")
            return None
        total, found = 0.0, False
        for f in fills:
            if f.get("coin") != coin or f.get("time", 0) / 1000 < since_ts:
                continue
            pnl = f.get("closedPnl")
            if pnl is not None:
                total += float(pnl)
                found = True
        return total if found else None

    def ensure_leverage(self, tok):
        coin = HL_COIN[tok]
        if DRY_RUN:
            print(f"  [DRY RUN] would set {coin} leverage={LEVERAGE_SETTING}x isolated")
            return
        self.exchange.update_leverage(LEVERAGE_SETTING, coin, is_cross=False)

    def open_market(self, tok, is_buy, sz):
        coin = HL_COIN[tok]
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} market {'BUY' if is_buy else 'SELL'} {sz:.6g} {coin}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.market_open(coin, is_buy, sz)

    def close_market(self, tok):
        coin = HL_COIN[tok]
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} market_close {coin}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.market_close(coin)

    def cancel_all(self, tok):
        coin = HL_COIN[tok]
        dex = "xyz" if ASSET_CLASS[tok] == "equity" else ""
        if DRY_RUN:
            print(f"  [DRY RUN] would cancel resting orders for {coin}")
            return
        for o in self.info.open_orders(ACCOUNT_ADDRESS, dex=dex):
            if o.get("coin") == coin:
                self.exchange.cancel(coin, o["oid"])

    def place_stop(self, tok, side, sz, stop_px):
        """Native resting stop protecting the position between daily runs."""
        coin = HL_COIN[tok]
        exit_is_buy = side != "long"
        limit_px = slippage_px(stop_px, exit_is_buy, TRIGGER_SLIP)
        order = {"coin": coin, "is_buy": exit_is_buy, "sz": sz, "limit_px": limit_px,
                 "order_type": {"trigger": {"isMarket": True, "triggerPx": round_sig(stop_px), "tpsl": "sl"}},
                 "reduce_only": True}
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} place stop for {coin}: trigger={round_sig(stop_px)}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.bulk_orders([order], grouping="na")


def portfolio_notional(state, mids, tokens):
    """tokens: the subset to sum over - crypto and equity notionals are
    checked against separate leverage caps (isolated-margin xyz dex, see
    module docstring), so this is never called over the full roster."""
    total = 0.0
    for tok in tokens:
        units = state["positions"].get(tok, {}).get("units", [])
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

    print("Updating daily candles from Binance (crypto) and Yahoo (equity/macro)...")
    candle_data = crypto_update_all(now_ts=now)
    candle_data.update(equity_update_all(now_ts=now))
    N_data = {tok: compute_N(candle_data[tok]) for tok in TOKENS}

    # Crypto and equity live on separate Hyperliquid perp dexes with
    # independent, isolated margin (see module docstring) - two account
    # snapshots, two risk-capital pools, merged live-position/mids dicts.
    us = trader.user_state()
    account_value = trader.account_value(us)
    live_pos = trader.live_positions(us)
    crypto_mids = with_retry(lambda: trader.info.all_mids())
    crypto_risk_capital = RISK_CAPITAL_FRACTION * account_value

    us_eq = trader.user_state(dex="xyz")
    equity_account_value = trader.account_value(us_eq, dex="xyz")
    live_pos.update(trader.live_positions(us_eq))
    equity_mids_raw = with_retry(lambda: trader.info.all_mids(dex="xyz"))
    equity_risk_capital = RISK_CAPITAL_FRACTION * equity_account_value

    mids = dict(crypto_mids)
    for k, v in equity_mids_raw.items():
        bare = k.split(":", 1)[-1]
        if bare in EQUITY_TOKENS:
            mids[bare] = v

    print(f"crypto: account_value=${account_value:.2f}  risk_capital=${crypto_risk_capital:.2f}")
    print(f"equity: account_value=${equity_account_value:.2f}  risk_capital=${equity_risk_capital:.2f}")
    print(f"open live positions: {sorted(live_pos.keys())}")

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
        risk_capital = crypto_risk_capital if ASSET_CLASS[tok] == "crypto" else equity_risk_capital
        pool_tokens = CRYPTO_TOKENS if ASSET_CLASS[tok] == "crypto" else EQUITY_TOKENS

        # Reconciliation against Hyperliquid's live position only makes sense
        # in real trading — DRY_RUN never places a real order in the first
        # place, so live_has_pos is always False for our own paper positions.
        # Treating that as "closed externally" (2026-09-06 incident: wiped out
        # a 3-token dry-run simulation after exactly one cycle, every time,
        # regardless of real market activity) was the bug; skip all of this
        # under DRY_RUN and let the normal stop/channel-exit/pyramid logic
        # below run against price data alone, exactly like the backtest does.
        if not DRY_RUN:
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
        elif not bot_has_pos and live_has_pos:
            # still worth flagging in dry-run: a real position exists on a
            # token this simulation is also tracking, so its notional isn't
            # actually free for the leverage-cap math the way this code
            # assumes. Doesn't block anything, just surfaces the conflict.
            print(f"  {tok}: NOTE real position exists but this is a dry-run paper position, ignoring for simulation")

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
                    prospective = portfolio_notional(state, mids, pool_tokens) + sz * trigger
                    if prospective <= LEVERAGE_CAP * risk_capital:
                        trader.open_market(tok, side == "long", sz)
                        st["units"].append({"side": side, "entry_px": trigger, "sz": sz,
                                            "n_at_entry": n, "entry_ts": now})
                        trader.cancel_all(tok)
                        trader.place_stop(tok, side, sum(u["sz"] for u in st["units"]), stop_price(st["units"]))
                        tg(f"Turtle/{tok}: pyramid unit {len(st['units'])} added @ ~{trigger:.6g}")
                    else:
                        print(f"  {tok}: pyramid triggered but portfolio leverage cap would be breached, skipping")
        elif tok in EQUITY_EXCLUDE:
            pass  # excluded from new entries - see EQUITY_EXCLUDE above
        else:
            hi_entry, lo_entry = hh(candles, i, ENTRY_N), ll(candles, i, ENTRY_N)
            long_sig, short_sig = c["high"] > hi_entry, c["low"] < lo_entry
            if long_sig or short_sig:
                side = "long" if long_sig else "short"
                entry_px = hi_entry if side == "long" else lo_entry
                sz = (RISK_PCT * risk_capital) / (n * DOLLARS_PER_POINT)
                prospective = portfolio_notional(state, mids, pool_tokens) + sz * entry_px
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

    equity_log.append({"ts": now, "equity": account_value, "equity_xyz": equity_account_value})
    save(EQUITY_FILE, equity_log)
    save(TRADES_FILE, trades)
    save(STATE_FILE, state)
    print("Done.")


if __name__ == "__main__":
    main()
