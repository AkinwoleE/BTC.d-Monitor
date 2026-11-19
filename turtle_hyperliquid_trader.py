#!/usr/bin/env python3
"""
Turtle System 2 on Hyperliquid, 28 instruments (18 crypto + 10 equity/macro).

System 2 only: 55-day Donchian entry, 20-day exit, 2N stop, add 0.5N up to
4 units. No System 1 skip filter. Signals come from completed daily candles
(Binance for crypto, Yahoo session closes for equity/macro). Orders go to
Hyperliquid: crypto on the main perp dex, equity/macro on the isolated xyz
HIP-3 dex. Each leg has its own account value, its own 0.25 risk-capital
fraction, and its own 3x notional cap. Leverage on the exchange is 3x
isolated.

A completed daily candle is acted on once per instrument. The mark is
`positions[token].last_candle_ts`. cron-job.org (job "Turtle Dry Run",
id 8401012, repository_dispatch turtle-cron-trigger) runs at 01:12
Africa/Lagos = 00:12 UTC, just after the 00:00 UTC Binance daily close.
GitHub's schedule backup is 00:10 UTC (`10 0 * * *`). Either one may run
first, and the GitHub one is often hours late, so both can see the same
candle. The second run finds the mark and does nothing.

Paper and live do not share state. HL_DRY_RUN defaults to true (repo
variable TURTLE_HL_DRY_RUN). Paper keeps writing the historical
turtle_hyperliquid_*.json files. Live writes the *.live.json files, which
start empty, so a paper book is never reconciled as CLOSED_EXTERNALLY.

Env: HYPERLIQUID_API_PRIVATE_KEY, HYPERLIQUID_ACCOUNT_ADDRESS, HL_DRY_RUN
("false" is live; anything else is paper), TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, optional TURTLE_EQUITY_EXCLUDE (default CL).
"""
import json, os, sys, time, traceback
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from divergence_monitor import tg
from turtle_backtest import compute_N, hh, ll, RISK_PCT, DOLLARS_PER_POINT, MAX_UNITS
from turtle_hyperliquid_lib import TOKENS as CRYPTO_TOKENS, update_all as crypto_update_all
from turtle_equity_lib import (
    TOKENS as EQUITY_TOKENS,
    HL_SYMBOL as EQUITY_HL_SYMBOL,
    update_all as equity_update_all,
    bar_is_complete,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIVE_FILE = os.path.join(HERE, "turtle_hyperliquid_active.json")

ENTRY_N, EXIT_N = 55, 20
RISK_CAPITAL_FRACTION = 0.25
LEVERAGE_CAP = 3
LEVERAGE_SETTING = 3              # isolated, set per market on the exchange
SLIPPAGE = 0.05                   # IOC bound used by market orders
TRIGGER_SLIP = 0.02               # limit cushion on the resting stop
MIN_NOTIONAL = 10.0               # Hyperliquid rejects orders below $10
PERP_DEXS = ["", "xyz"]           # "" is the main perp dex; xyz is the HIP-3 book

# Unset means exclude CL (the backtest outlier). The workflow passes the
# TURTLE_EQUITY_EXCLUDE repo variable and substitutes CL when it is empty,
# so clearing the variable does not start trading crude. Set it to NONE
# (not on the roster) to exclude nothing, or to a comma-separated list.
if "TURTLE_EQUITY_EXCLUDE" in os.environ:
    _exclude_raw = os.environ["TURTLE_EQUITY_EXCLUDE"]
else:
    _exclude_raw = "CL"
EQUITY_EXCLUDE = {t.strip().upper() for t in _exclude_raw.split(",") if t.strip()}

DRY_RUN = os.environ.get("HL_DRY_RUN", "true").strip().lower() != "false"
PRIVATE_KEY = os.environ.get("HYPERLIQUID_API_PRIVATE_KEY", "")
ACCOUNT_ADDRESS = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "")

TOKENS = CRYPTO_TOKENS + EQUITY_TOKENS
ASSET_CLASS = {tok: "crypto" for tok in CRYPTO_TOKENS}
ASSET_CLASS.update({tok: "equity" for tok in EQUITY_TOKENS})
HL_COIN = {tok: tok for tok in CRYPTO_TOKENS}
HL_COIN.update(EQUITY_HL_SYMBOL)
REVERSE_HL_COIN = {v: k for k, v in HL_COIN.items()}


def book_paths(dry_run, here=HERE):
    """Paper keeps the historical filenames. Live uses a separate trio so
    switching HL_DRY_RUN on does not read the paper book as real positions."""
    if dry_run:
        names = (
            "turtle_hyperliquid_state.json",
            "turtle_hyperliquid_trades.json",
            "turtle_hyperliquid_equity.json",
        )
    else:
        names = (
            "turtle_hyperliquid_state.live.json",
            "turtle_hyperliquid_trades.live.json",
            "turtle_hyperliquid_equity.live.json",
        )
    return tuple(os.path.join(here, name) for name in names)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def html_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def format_alert(msg, dry_run):
    if dry_run:
        return "📝 <b>DRY RUN / PAPER</b>\n" + msg
    return "🔴 <b>LIVE</b>\n" + msg


def notify(msg, dry_run=None):
    if dry_run is None:
        dry_run = DRY_RUN
    text = format_alert(msg, dry_run)
    print(f"  TELEGRAM: {text}")
    try:
        tg(text)
    except Exception as e:
        print(f"  Telegram error: {e}")


def day_label(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def round_sz(sz, sz_decimals):
    """Lot size, rounded down so a unit never exceeds the 1% risk budget
    and never gains a digit the exchange will reject."""
    d = int(sz_decimals)
    if sz <= 0 or d < 0:
        return 0.0
    quant = Decimal("1") if d == 0 else Decimal(f"1e-{d}")
    return float(Decimal(str(sz)).quantize(quant, rounding=ROUND_DOWN))


def round_px(px, sz_decimals, max_decimals=6):
    """Perp price: 5 significant figures, and at most (6 - szDecimals)
    decimal places. Prices above 100_000 are integers. The result is a
    float Hyperliquid's float_to_wire will accept."""
    px = float(px)
    if px <= 0:
        raise ValueError(f"price must be positive, got {px}")
    if px > 100_000:
        rounded = float(int(round(px)))
    else:
        decimals = max(0, max_decimals - int(sz_decimals))
        rounded = round(float(f"{px:.5g}"), decimals)
    snapped = float(f"{rounded:.8f}")
    if abs(snapped - rounded) >= 1e-12:
        rounded = snapped
    return rounded


def size_unit(risk_capital, n, px, sz_decimals, slippage=SLIPPAGE):
    """Return (size, None) or (None, reason).

    Size is 1% of risk capital per 1N. The order is skipped when that size
    is not a positive lot, or when the worse-case IOC notional (a sell is
    priced `slippage` through the reference) is under Hyperliquid's $10
    minimum. A zero account used to pass the leverage cap because
    0 <= 0, and the exchange then stored a zero-size position.
    """
    if risk_capital is None or risk_capital <= 0:
        return None, "risk capital is zero"
    if n is None or n <= 0 or px is None or px <= 0:
        return None, "invalid N or price"
    if sz_decimals is None:
        return None, "szDecimals unavailable"
    raw = (RISK_PCT * float(risk_capital)) / (float(n) * DOLLARS_PER_POINT)
    sz = round_sz(raw, sz_decimals)
    if sz <= 0:
        return None, f"size {raw:.6g} rounds to 0 at szDecimals={int(sz_decimals)}"
    worst_notional = sz * float(px) * (1 - slippage)
    if worst_notional < MIN_NOTIONAL:
        return None, (
            f"notional ${sz * float(px):.2f} is under the ${MIN_NOTIONAL:.0f} "
            f"minimum after {slippage:.0%} slippage"
        )
    return sz, None


def min_account_for_unit(n_over_price, slippage=SLIPPAGE):
    """Account value at which one unit's worse-case notional equals $10.
    `n_over_price` is N/price (daily true-range as a fraction of price)."""
    if n_over_price <= 0:
        raise ValueError("N/price must be positive")
    per_dollar = RISK_PCT * RISK_CAPITAL_FRACTION * (1.0 / n_over_price) * (1 - slippage)
    return MIN_NOTIONAL / per_dollar


def trigger_prices(stop_px, exit_is_buy, sz_decimals):
    raw_limit = stop_px * (1 + TRIGGER_SLIP) if exit_is_buy else stop_px * (1 - TRIGGER_SLIP)
    return round_px(stop_px, sz_decimals), round_px(raw_limit, sz_decimals)


def stop_price(units):
    latest = units[-1]
    n = latest["n_at_entry"]
    return latest["entry_px"] - 2 * n if latest["side"] == "long" else latest["entry_px"] + 2 * n


def completed_candles(tok, candles, now_ts):
    if ASSET_CLASS[tok] == "equity":
        return [c for c in candles if bar_is_complete(c["ts"], now_ts)]
    return [c for c in candles if c["ts"] + 86400 <= now_ts]


def portfolio_notional(state, mids, tokens):
    total = 0.0
    for tok in tokens:
        units = state.get("positions", {}).get(tok, {}).get("units", [])
        if not units:
            continue
        raw = mids.get(tok, units[-1]["entry_px"])
        try:
            px = float(raw)
        except (TypeError, ValueError):
            px = float(units[-1]["entry_px"])
        total += sum(float(u["sz"]) for u in units) * px
    return total


def parse_order_status(result):
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected order result: {result!r}")
    if result.get("dry_run"):
        return {"dry_run": True, "oid": result.get("oid")}
    if result.get("status") not in ("ok", "success"):
        raise RuntimeError(f"order rejected: {result}")
    response = result.get("response") or {}
    data = response.get("data") or {}
    statuses = data.get("statuses") or []
    if not statuses:
        return {"ok": True, "oid": result.get("oid")}
    st = statuses[0]
    if isinstance(st, str):
        raise RuntimeError(st)
    if "error" in st:
        raise RuntimeError(st["error"])
    if "filled" in st:
        filled = st["filled"]
        return {
            "oid": filled.get("oid"),
            "px": float(filled["avgPx"]),
            "sz": float(filled["totalSz"]),
        }
    if "resting" in st:
        return {"oid": st["resting"].get("oid")}
    raise RuntimeError(f"unrecognized order status: {st}")


def executed_fill(result, dry_run, fallback_px, fallback_sz):
    if dry_run:
        return {"px": float(fallback_px), "sz": float(fallback_sz), "oid": None}
    parsed = parse_order_status(result)
    if parsed.get("dry_run") or parsed.get("px") is None or parsed.get("sz") is None:
        raise RuntimeError(f"order did not fill: {result}")
    if float(parsed["sz"]) <= 0:
        raise RuntimeError("filled size is 0")
    return {"px": float(parsed["px"]), "sz": float(parsed["sz"]), "oid": parsed.get("oid")}


def upsert_stop(trader, tok, side, sz, stop_px, existing_oid, sz_decimals):
    """Rest a stop for the whole position.

    A pyramid used to cancel the old stop and then place the new one, which
    left the position naked between the two calls. The old stop is modified
    in place when its oid is known. If the modify is rejected, the new stop
    is accepted before the old one is cancelled, so the position is never
    unstopped — the overlap is two reduce-only stops, not zero.
    """
    if sz_decimals is None:
        raise RuntimeError("szDecimals unknown; not placing a stop")
    if stop_px is None or stop_px <= 0:
        raise RuntimeError(f"stop price {stop_px} is not placeable")
    exit_is_buy = side != "long"
    trigger_px, limit_px = trigger_prices(stop_px, exit_is_buy, sz_decimals)
    sz = round_sz(sz, sz_decimals)
    if sz <= 0:
        raise RuntimeError(f"stop size rounds to 0 at szDecimals={int(sz_decimals)}")
    if existing_oid:
        try:
            res = trader.modify_stop(tok, existing_oid, exit_is_buy, sz, limit_px, trigger_px)
            parsed = parse_order_status(res)
            return parsed.get("oid") or existing_oid
        except Exception as e:
            print(f"  {tok}: stop modify failed ({e}); placing the new stop before cancelling the old one")
    res = trader.place_stop(tok, exit_is_buy, sz, limit_px, trigger_px)
    parsed = parse_order_status(res)
    new_oid = parsed.get("oid")
    if existing_oid and new_oid and new_oid != existing_oid:
        trader.cancel_oid(tok, existing_oid)
    return new_oid


def reconciliation_action(dry_run, units, live_pos):
    """Paper never treats a missing exchange position as a close. Live does,
    and it stands down when the exchange has a position this book didn't open."""
    if dry_run:
        return None
    bot_has = bool(units)
    live_has = live_pos is not None and float(live_pos.get("szi", 0)) != 0
    if bot_has and not live_has:
        return "close_external"
    if not bot_has and live_has:
        return "stand_down"
    if bot_has and live_has:
        side_actual = "long" if float(live_pos["szi"]) > 0 else "short"
        if units[0]["side"] != side_actual:
            return "side_mismatch"
    return None


def net_from_fills(fills):
    if not fills:
        return None, None
    gross, fees, saw_pnl = 0.0, 0.0, False
    for fill in fills:
        if fill.get("closedPnl") is not None:
            gross += float(fill["closedPnl"])
            saw_pnl = True
        if fill.get("fee") is not None:
            fees += float(fill["fee"])
    if not saw_pnl:
        return None, (fees if fees else None)
    return gross - fees, fees


def decide(tok, candles, n_series, st, risk_capital, mids, state, pool_tokens, excluded, sz_decimals):
    """Pure decision for the latest candle in `candles` (already complete).

    `last_candle_ts` missing → seed (record the candle, trade nothing).
    Equal to this candle → already processed. Otherwise at most one of
    close / add / open, matching the backtest's once-through-the-bar loop.
    """
    i = len(candles) - 1
    c = candles[i]
    n = n_series[i] if n_series is not None and i < len(n_series) else None
    ts = c["ts"]
    if "last_candle_ts" not in st:
        return {"op": "seed", "candle_ts": ts}
    if st.get("last_candle_ts") == ts:
        return {"op": "already", "candle_ts": ts}
    if len(candles) < ENTRY_N + 5 or n is None or n <= 0:
        return {"op": "skip", "consume": False, "candle_ts": ts, "reason": "not enough history"}

    if st.get("units"):
        side = st["units"][0]["side"]
        stop = stop_price(st["units"])
        stopped = (side == "long" and c["low"] <= stop) or (side == "short" and c["high"] >= stop)
        if side == "long":
            exit_lvl = ll(candles, i, EXIT_N)
            channel_hit = c["low"] <= exit_lvl
        else:
            exit_lvl = hh(candles, i, EXIT_N)
            channel_hit = c["high"] >= exit_lvl
        if stopped or channel_hit:
            return {
                "op": "close", "candle_ts": ts, "side": side,
                "reason": "STOP" if stopped else "CHANNEL_EXIT",
                "exit_px": stop if stopped else exit_lvl,
            }
        if len(st["units"]) < MAX_UNITS:
            last_fill = st["units"][-1]["entry_px"]
            trigger = last_fill + 0.5 * n if side == "long" else last_fill - 0.5 * n
            add = (side == "long" and c["high"] >= trigger) or (side == "short" and c["low"] <= trigger)
            if add:
                return _sized_order(
                    "add", tok, side, trigger, n, ts, risk_capital, mids, state,
                    pool_tokens, sz_decimals,
                )
        return {"op": "hold", "candle_ts": ts}

    if tok in excluded:
        return {"op": "hold", "candle_ts": ts, "reason": "excluded from new entries"}

    hi_entry, lo_entry = hh(candles, i, ENTRY_N), ll(candles, i, ENTRY_N)
    long_sig = c["high"] > hi_entry
    short_sig = c["low"] < lo_entry
    if not (long_sig or short_sig):
        return {"op": "hold", "candle_ts": ts}
    side = "long" if long_sig else "short"
    entry_px = hi_entry if side == "long" else lo_entry
    return _sized_order(
        "open", tok, side, entry_px, n, ts, risk_capital, mids, state,
        pool_tokens, sz_decimals,
    )


def _sized_order(op, tok, side, px, n, ts, risk_capital, mids, state, pool_tokens, sz_decimals):
    sz, why = size_unit(risk_capital, n, px, sz_decimals)
    if sz is None:
        return {"op": "skip", "consume": True, "candle_ts": ts, "reason": f"{op} skipped: {why}"}
    prospective = portfolio_notional(state, mids, pool_tokens) + sz * px
    cap = LEVERAGE_CAP * float(risk_capital)
    if prospective > cap + 1e-6:
        return {
            "op": "skip", "consume": True, "candle_ts": ts,
            "reason": f"{op} skipped: notional ${prospective:.2f} would exceed the ${cap:.2f} cap",
        }
    return {"op": op, "candle_ts": ts, "side": side, "sz": sz, "px": px, "n": n}


def _blank_result():
    return {"messages": [], "logs": [], "changed": False, "seed": False, "skip_reason": None}


def apply_token(tok, candles, n_series, state, trades, *, now, risk_capital, mids,
                pool_tokens, trader, sz_decimals, excluded, dry_run, live_pos,
                persist, live_known=True):
    """Run one instrument. Persist immediately after any fill is recorded
    and after the candle mark moves, so a later crash cannot re-trade it
    or forget an order that already went out."""
    st = state["positions"].setdefault(tok, {})
    st.setdefault("units", [])
    out = _blank_result()

    def touch():
        out["changed"] = True
        persist()

    if not live_known:
        # A failed xyz snapshot must not look like a flat account: that would
        # either skip-and-consume today's signal or, in live mode, flatten
        # the book as CLOSED_EXTERNALLY. Leave the candle unmarked.
        out["logs"].append(f"  {tok}: exchange snapshot unavailable; not trading and not reconciling")
        return out

    if not st["units"] and live_pos is not None:
        bot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot")
        if bot not in sys.path:
            sys.path.insert(0, bot)
        from hedge_book import dec_signed_size, ignore_for_other_bot, load_book
        rows, source = load_book()
        account = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "")
        opposite = dec_signed_size(tok) if str(tok).upper() == "BTC" else None
        if ignore_for_other_bot(
            False, "hyperliquid", tok, live_pos.get("szi"), rows, account=account,
            book_source=source, opposite_size=opposite,
        ):
            out["logs"].append(f"  {tok}: hedge-bot position ignored")
            live_pos = None

    if dry_run and live_pos and not st["units"]:
        out["logs"].append(f"  {tok}: NOTE a real position exists; paper mode leaves it alone")

    action = reconciliation_action(dry_run, st["units"], live_pos)
    if action == "close_external":
        _record_external_close(tok, st, trades, trader, now, out)
        if candles:
            st["last_candle_ts"] = candles[-1]["ts"]
        touch()
        try:
            trader.cancel_orders(tok)
        except Exception as e:
            out["logs"].append(f"  {tok}: cancel after external close failed ({e})")
        return out
    if action == "stand_down":
        out["messages"].append(
            f"⚠️ Turtle/{tok}: found an open Hyperliquid position this bot didn't open. "
            f"Standing down on {tok}."
        )
        return out
    if action == "side_mismatch":
        out["messages"].append(
            f"⚠️ Turtle/{tok}: live position side doesn't match this book's state. "
            f"Standing down on {tok}."
        )
        return out

    if not candles:
        out["logs"].append(f"  {tok}: no completed candle")
        return out

    plan = decide(
        tok, candles, n_series, st, risk_capital, mids, state, pool_tokens, excluded, sz_decimals,
    )
    op = plan["op"]
    label = day_label(plan["candle_ts"])

    if op == "seed":
        st["last_candle_ts"] = plan["candle_ts"]
        touch()
        out["seed"] = True
        out["logs"].append(f"  {tok}: seeded last candle {label}; no order on this bar")
        return out

    if op == "already":
        out["logs"].append(f"  {tok}: candle {label} already processed")
        _maybe_repair_stop(tok, st, trader, sz_decimals, dry_run, out, touch)
        return out

    if op == "skip":
        out["logs"].append(f"  {tok}: {plan['reason']}")
        if plan.get("consume"):
            st["last_candle_ts"] = plan["candle_ts"]
            out["skip_reason"] = plan["reason"]
            touch()
        return out

    if op == "hold":
        extra = f" ({plan['reason']})" if plan.get("reason") else ""
        out["logs"].append(f"  {tok}: hold {label}{extra}")
        st["last_candle_ts"] = plan["candle_ts"]
        touch()
        _maybe_repair_stop(tok, st, trader, sz_decimals, dry_run, out, touch)
        return out

    if op == "close":
        _execute_close(tok, st, trades, trader, plan, now, dry_run, out, touch)
        return out

    if op in ("open", "add"):
        _execute_entry(tok, st, trader, plan, now, dry_run, sz_decimals, out, touch)
        return out

    raise RuntimeError(f"unknown plan {op}")


def _record_external_close(tok, st, trades, trader, now, out):
    first = st["units"][0]
    fills = []
    if hasattr(trader, "fills_since"):
        try:
            # entry_ts is stamped after the fill comes back, so the fill's own
            # timestamp is a little earlier. Two minutes of slack still cannot
            # reach the previous day's trade on this daily system.
            since = first["entry_ts"] - 120 if first.get("entry_ts") else 0
            fills = trader.fills_since(tok, since) or []
        except Exception as e:
            out["logs"].append(f"  {tok}: fill lookup failed ({e})")
    net, fees = net_from_fills(fills)
    held_h = (now - first["entry_ts"]) / 3600 if first.get("entry_ts") else None
    trade = {
        "token": tok, "side": first["side"], "units": len(st["units"]),
        "entry_ts": first.get("entry_ts"), "exit_ts": now,
        "held_hours": None if held_h is None else round(held_h, 1),
        "pnl": None if net is None else round(net, 2),
        "reason": "CLOSED_EXTERNALLY",
    }
    if fees is not None:
        trade["fees"] = round(fees, 4)
    trades.append(trade)
    pnl_txt = "unknown" if net is None else f"${net:+.2f}"
    out["messages"].append(
        f"Turtle/{tok}: position is no longer on Hyperliquid "
        f"(stop filled or closed outside this bot). PnL: {pnl_txt}"
    )
    out["logs"].append(f"  {tok}: CLOSED_EXTERNALLY pnl={pnl_txt}")
    st["units"] = []
    st.pop("stop_oid", None)


def _execute_close(tok, st, trades, trader, plan, now, dry_run, out, touch):
    # Close first, cancel the resting stop after. A failed close leaves the
    # stop in place; cancelling first would open a gap.
    result = trader.close_market(tok)
    fallback_sz = sum(float(u["sz"]) for u in st["units"])
    fill = executed_fill(result, dry_run, plan["exit_px"], fallback_sz)
    exit_px = fill["px"]
    exit_fee = _lookup_fee(trader, tok, fill.get("oid"), dry_run)
    side = plan["side"]
    gross = 0.0
    entry_fees = 0.0
    for u in st["units"]:
        move = (exit_px - u["entry_px"]) if side == "long" else (u["entry_px"] - exit_px)
        gross += move * float(u["sz"])
        if u.get("fee") is not None:
            entry_fees += float(u["fee"])
    fees = entry_fees + (exit_fee or 0.0)
    net = gross - fees
    first = st["units"][0]
    held_h = (now - first["entry_ts"]) / 3600 if first.get("entry_ts") else None
    trade = {
        "token": tok, "side": side, "units": len(st["units"]),
        "entry_ts": first.get("entry_ts"), "exit_ts": now,
        "held_hours": None if held_h is None else round(held_h, 1),
        "pnl": round(net, 2), "reason": plan["reason"],
        "exit_px": exit_px, "candle_ts": plan["candle_ts"],
    }
    if fees:
        trade["fees"] = round(fees, 4)
    trades.append(trade)
    st["units"] = []
    st.pop("stop_oid", None)
    st["last_candle_ts"] = plan["candle_ts"]
    touch()
    try:
        trader.cancel_orders(tok)
    except Exception as e:
        out["messages"].append(
            f"⚠️ Turtle/{tok}: closed, but cancelling the resting stop failed "
            f"({html_escape(e)})."
        )
    emoji = "🛑" if plan["reason"] == "STOP" else "✅"
    fee_bit = f"  fees ${fees:.4f}" if fees else ""
    fill_bit = "" if dry_run else " (fill)"
    out["messages"].append(
        f"{emoji} <b>Turtle/{tok} closed</b> ({plan['reason']}): "
        f"pnl ${net:+.2f} @ {exit_px:.6g}{fill_bit}{fee_bit}"
    )
    out["logs"].append(
        f"  {tok}: {plan['reason']} {day_label(plan['candle_ts'])} pnl={net:+.2f}"
    )


def _execute_entry(tok, st, trader, plan, now, dry_run, sz_decimals, out, touch):
    side = plan["side"]
    if plan["op"] == "open":
        trader.ensure_leverage(tok)
    result = trader.open_market(tok, side == "long", plan["sz"])
    fill = executed_fill(result, dry_run, plan["px"], plan["sz"])
    fee = _lookup_fee(trader, tok, fill.get("oid"), dry_run)
    unit = {
        "side": side,
        "entry_px": fill["px"],
        "sz": fill["sz"],
        "n_at_entry": plan["n"],
        "entry_ts": now,
        "signal_px": plan["px"],
        "candle_ts": plan["candle_ts"],
    }
    if fee is not None:
        unit["fee"] = fee
    if plan["op"] == "open":
        st["units"] = [unit]
    else:
        st["units"].append(unit)
    st["last_candle_ts"] = plan["candle_ts"]
    # The fill is on the book before the stop call. A crash in the stop
    # path must not lose the unit or forget that this candle was traded.
    touch()
    try:
        oid = upsert_stop(
            trader, tok, side, sum(float(u["sz"]) for u in st["units"]),
            stop_price(st["units"]), st.get("stop_oid"), sz_decimals,
        )
        if oid is not None:
            st["stop_oid"] = oid
        touch()
    except Exception as e:
        out["messages"].append(
            f"⚠️ Turtle/{tok}: the unit is on, but the protective stop was not updated "
            f"({html_escape(e)})."
        )
        out["logs"].append(f"  {tok}: stop update failed ({e})")
    px = unit["entry_px"]
    stop = stop_price(st["units"])
    fill_bit = "" if dry_run else " fill"
    fee_bit = f"  fee ${fee:.4f}" if fee is not None else ""
    if plan["op"] == "open":
        arrow = "🔼" if side == "long" else "🔻"
        out["messages"].append(
            f"{arrow} <b>Turtle/{tok} opened {side.upper()}</b>\n"
            f"Size: {unit['sz']:.6g} @ {px:.6g}{fill_bit}  Stop: {stop:.6g}{fee_bit}"
        )
    else:
        out["messages"].append(
            f"Turtle/{tok}: pyramid unit {len(st['units'])} "
            f"added @ {px:.6g}{fill_bit}{fee_bit}"
        )
    out["logs"].append(
        f"  {tok}: {plan['op']} {side} {day_label(plan['candle_ts'])} "
        f"sz={unit['sz']:.6g} @ {px:.6g}"
    )


def _lookup_fee(trader, tok, oid, dry_run):
    if dry_run or oid is None or not hasattr(trader, "fee_for_fill"):
        return None
    try:
        fee = trader.fee_for_fill(HL_COIN[tok], oid)
    except Exception as e:
        print(f"  {tok}: fee lookup failed ({e})")
        return None
    return None if fee is None else float(fee)


def _maybe_repair_stop(tok, st, trader, sz_decimals, dry_run, out, touch):
    if dry_run or not st.get("units") or st.get("stop_oid") or sz_decimals is None:
        return
    if hasattr(trader, "find_stop_oid"):
        try:
            found = trader.find_stop_oid(tok)
        except Exception as e:
            out["logs"].append(f"  {tok}: stop lookup failed ({e})")
            found = None
        if found:
            st["stop_oid"] = found
            touch()
            out["logs"].append(f"  {tok}: adopted resting stop oid={found}")
            return
    try:
        oid = upsert_stop(
            trader, tok, st["units"][0]["side"],
            sum(float(u["sz"]) for u in st["units"]),
            stop_price(st["units"]), None, sz_decimals,
        )
    except Exception as e:
        out["messages"].append(
            f"⚠️ Turtle/{tok}: open position has no resting stop ({html_escape(e)})."
        )
        return
    if oid is not None:
        st["stop_oid"] = oid
        touch()


def process_all(items, handle, on_error):
    """One instrument's exception is reported and the loop continues."""
    failed = []
    for item in items:
        try:
            handle(item)
        except Exception as e:
            failed.append(item)
            on_error(item, e)
    return failed


def with_retry(fn, retries=3, delay=2):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  transient error ({e}), retrying in {delay}s...")
            time.sleep(delay)


def _coins_match(a, b):
    return a == b or str(a).split(":")[-1] == str(b).split(":")[-1]


class Trader:
    def __init__(self):
        # perp_dexs loads xyz:COIN into the asset map. Without it, market
        # orders and stops on the equity leg raise "unknown coin".
        # perp_dexs loads xyz:COIN into the asset map. Without it, orders on
        # the equity leg raise "unknown coin". Mutators return before using
        # self.exchange while DRY_RUN is set.
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=PERP_DEXS)
        self.exchange = None
        if PRIVATE_KEY and ACCOUNT_ADDRESS:
            wallet = Account.from_key(PRIVATE_KEY)
            self.exchange = Exchange(
                wallet, base_url=constants.MAINNET_API_URL,
                account_address=ACCOUNT_ADDRESS, perp_dexs=PERP_DEXS,
            )

    def load_sz_decimals(self):
        out = {}
        for dex in ("", "xyz"):
            meta = with_retry(lambda dex=dex: self.info.meta(dex=dex))
            for asset in meta.get("universe", []):
                out[asset["name"]] = int(asset["szDecimals"])
        return out

    def user_state(self, dex=""):
        return with_retry(lambda: self.info.user_state(ACCOUNT_ADDRESS, dex=dex))

    def account_value(self, us, dex=""):
        """The main dex falls back to spot USDC when accountValue is 0.
        A named dex must not: xyz collateral is a separate pool, and
        reporting spot USDC as xyz equity would size equity orders against
        money that is not posted there."""
        cross_val = float(us.get("marginSummary", {}).get("accountValue", 0))
        if cross_val > 0 or dex:
            return cross_val
        try:
            spot = self.info.spot_user_state(ACCOUNT_ADDRESS)
            usdc = next((b for b in spot.get("balances", []) if b.get("coin") == "USDC"), None)
            return float(usdc["total"]) if usdc else 0.0
        except Exception as e:
            print(f"  could not fetch spot balance: {e}")
            return cross_val

    def live_positions(self, us):
        out = {}
        for ap in us.get("assetPositions", []):
            pos = ap["position"]
            if float(pos["szi"]) == 0:
                continue
            coin = pos["coin"]
            bare = coin.split(":", 1)[-1]
            tok = REVERSE_HL_COIN.get(coin) or REVERSE_HL_COIN.get(f"xyz:{bare}") or (
                bare if bare in CRYPTO_TOKENS else None
            )
            if tok:
                out[tok] = pos
            else:
                print(f"  NOTE: live position on unrecognized coin '{coin}'")
        return out

    def fills_since(self, tok, since_ts):
        coin = HL_COIN[tok]
        fills = with_retry(lambda: self.info.user_fills(ACCOUNT_ADDRESS))
        return [
            f for f in fills
            if _coins_match(f.get("coin"), coin) and f.get("time", 0) / 1000 >= since_ts
        ]

    def fee_for_fill(self, coin, oid, tries=3):
        if oid is None:
            return None
        for i in range(tries):
            fills = with_retry(lambda: self.info.user_fills(ACCOUNT_ADDRESS))
            fee, found = 0.0, False
            for f in fills:
                if f.get("oid") == oid and _coins_match(f.get("coin"), coin) and f.get("fee") is not None:
                    fee += float(f["fee"])
                    found = True
            if found:
                return fee
            if i < tries - 1:
                time.sleep(0.4)
        return None

    def _dex(self, tok):
        return "xyz" if ASSET_CLASS[tok] == "equity" else ""

    def ensure_leverage(self, tok):
        coin = HL_COIN[tok]
        if DRY_RUN:
            print(f"  [DRY RUN] would set {coin} leverage={LEVERAGE_SETTING}x isolated")
            return
        self.exchange.update_leverage(LEVERAGE_SETTING, coin, is_cross=False)

    def open_market(self, tok, is_buy, sz):
        coin = HL_COIN[tok]
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} market {'BUY' if is_buy else 'SELL'} {sz:.8g} {coin}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.market_open(coin, is_buy, sz, slippage=SLIPPAGE)

    def close_market(self, tok):
        coin = HL_COIN[tok]
        print(f"  {'[DRY RUN] would' if DRY_RUN else ''} market_close {coin}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.market_close(coin, slippage=SLIPPAGE)

    def cancel_orders(self, tok):
        coin = HL_COIN[tok]
        if DRY_RUN:
            print(f"  [DRY RUN] would cancel resting orders for {coin}")
            return
        try:
            orders = self.info.frontend_open_orders(ACCOUNT_ADDRESS, dex=self._dex(tok))
        except Exception as e:
            print(f"  {tok}: frontend_open_orders failed ({e}); falling back to open_orders")
            orders = self.info.open_orders(ACCOUNT_ADDRESS, dex=self._dex(tok))
        for o in orders:
            if _coins_match(o.get("coin"), coin) and o.get("oid") is not None:
                self.exchange.cancel(coin, o["oid"])

    def cancel_oid(self, tok, oid):
        coin = HL_COIN[tok]
        if DRY_RUN:
            print(f"  [DRY RUN] would cancel oid {oid} on {coin}")
            return
        self.exchange.cancel(coin, oid)

    def find_stop_oid(self, tok):
        if DRY_RUN:
            return None
        coin = HL_COIN[tok]
        try:
            orders = self.info.frontend_open_orders(ACCOUNT_ADDRESS, dex=self._dex(tok))
        except Exception as e:
            print(f"  {tok}: frontend_open_orders failed ({e})")
            return None
        trigger_oid = None
        reduce_oid = None
        for o in orders:
            if not _coins_match(o.get("coin"), coin):
                continue
            if o.get("isTrigger") or str(o.get("orderType", "")).lower().startswith("stop"):
                trigger_oid = o.get("oid")
            elif o.get("reduceOnly"):
                reduce_oid = o.get("oid")
        return trigger_oid or reduce_oid

    def _stop_order(self, tok, is_buy, sz, limit_px, trigger_px):
        coin = HL_COIN[tok]
        return {
            "coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": limit_px,
            "order_type": {"trigger": {"isMarket": True, "triggerPx": trigger_px, "tpsl": "sl"}},
            "reduce_only": True,
        }

    def place_stop(self, tok, is_buy, sz, limit_px, trigger_px):
        coin = HL_COIN[tok]
        print(f"  {'[DRY RUN] would place' if DRY_RUN else 'placing'} stop {coin} "
              f"trigger={trigger_px} sz={sz}")
        if DRY_RUN:
            return {"dry_run": True}
        return self.exchange.bulk_orders([self._stop_order(tok, is_buy, sz, limit_px, trigger_px)], grouping="na")

    def modify_stop(self, tok, oid, is_buy, sz, limit_px, trigger_px):
        coin = HL_COIN[tok]
        print(f"  {'[DRY RUN] would modify' if DRY_RUN else 'modifying'} stop {coin} "
              f"oid={oid} trigger={trigger_px} sz={sz}")
        if DRY_RUN:
            return {"dry_run": True, "oid": oid}
        return self.exchange.modify_order(
            oid, coin, is_buy, sz, limit_px,
            {"trigger": {"isMarket": True, "triggerPx": trigger_px, "tpsl": "sl"}},
            reduce_only=True,
        )


def _ensure_book(state):
    if not isinstance(state, dict):
        state = {}
    state.setdefault("positions", {})
    for tok in TOKENS:
        pos = state["positions"].setdefault(tok, {})
        if not isinstance(pos, dict):
            state["positions"][tok] = {"units": []}
            continue
        pos.setdefault("units", [])
    return state


def main():
    now = int(time.time())
    mode = "paper" if DRY_RUN else "live"
    print(f"turtle_hyperliquid_trader {datetime.now(timezone.utc).isoformat()} "
          f"mode={mode} instruments={len(TOKENS)} "
          f"risk_capital_fraction={RISK_CAPITAL_FRACTION} leverage_cap={LEVERAGE_CAP}x isolated")
    state_path, trades_path, equity_path = book_paths(DRY_RUN)
    save(ACTIVE_FILE, {"mode": mode})

    if not PRIVATE_KEY or not ACCOUNT_ADDRESS:
        print("  HYPERLIQUID_API_PRIVATE_KEY / HYPERLIQUID_ACCOUNT_ADDRESS not set — reads that need the account will fail")

    print("Updating daily candles (Binance crypto, Yahoo equity/macro)...")
    raw = crypto_update_all(now_ts=now)
    raw.update(equity_update_all(now_ts=now))

    trader = Trader()
    state = _ensure_book(load(state_path, {"positions": {}}))
    state["mode"] = mode
    trades = load(trades_path, [])
    if not isinstance(trades, list):
        trades = []
    equity_log = load(equity_path, [])
    if not isinstance(equity_log, list):
        equity_log = []

    def persist():
        save(state_path, state)
        save(trades_path, trades)

    try:
        specs = trader.load_sz_decimals()
    except Exception as e:
        print(f"  could not load szDecimals ({e}); new orders will be skipped")
        notify(f"⚠️ Could not load Hyperliquid szDecimals ({html_escape(e)}). New units are skipped this run.")
        specs = {}

    us = trader.user_state()
    account_value = trader.account_value(us)
    live_pos = trader.live_positions(us)
    crypto_mids = with_retry(lambda: trader.info.all_mids())
    crypto_risk = RISK_CAPITAL_FRACTION * account_value

    equity_snapshot_ok = True
    try:
        us_eq = trader.user_state(dex="xyz")
        equity_account_value = trader.account_value(us_eq, dex="xyz")
        live_pos.update(trader.live_positions(us_eq))
        equity_mids_raw = with_retry(lambda: trader.info.all_mids(dex="xyz"))
    except Exception as e:
        equity_snapshot_ok = False
        equity_account_value = 0.0
        equity_mids_raw = {}
        print(f"  xyz snapshot failed ({e}); equity leg skipped this run")
        notify(f"⚠️ xyz dex snapshot failed ({html_escape(e)}). Equity/macro instruments are skipped this run; nothing on that leg was flattened.")

    equity_risk = RISK_CAPITAL_FRACTION * equity_account_value
    mids = {k: v for k, v in crypto_mids.items()}
    for k, v in equity_mids_raw.items():
        bare = str(k).split(":", 1)[-1]
        if bare in EQUITY_TOKENS:
            mids[bare] = v

    print(f"crypto: account_value=${account_value:.2f}  risk_capital=${crypto_risk:.2f}")
    print(f"equity: account_value=${equity_account_value:.2f}  risk_capital=${equity_risk:.2f}  snapshot_ok={equity_snapshot_ok}")
    print(f"open live positions: {sorted(live_pos)}")

    candle_data, n_data = {}, {}
    for tok in TOKENS:
        done = completed_candles(tok, raw.get(tok, []), now)
        candle_data[tok] = done
        n_data[tok] = compute_N(done) if done else []

    seeds, skips = [], []

    def handle(tok):
        pool = CRYPTO_TOKENS if ASSET_CLASS[tok] == "crypto" else EQUITY_TOKENS
        risk = crypto_risk if ASSET_CLASS[tok] == "crypto" else equity_risk
        result = apply_token(
            tok, candle_data[tok], n_data[tok], state, trades,
            now=now, risk_capital=risk, mids=mids, pool_tokens=pool,
            trader=trader, sz_decimals=specs.get(HL_COIN[tok]),
            excluded=EQUITY_EXCLUDE, dry_run=DRY_RUN,
            live_pos=live_pos.get(tok), persist=persist,
            live_known=True if ASSET_CLASS[tok] == "crypto" else equity_snapshot_ok,
        )
        for line in result["logs"]:
            print(line)
        for msg in result["messages"]:
            notify(msg)
        if result.get("seed"):
            seeds.append(tok)
        if result.get("skip_reason"):
            skips.append(f"{tok}: {result['skip_reason']}")

    def on_error(tok, err):
        traceback.print_exc()
        print(f"  {tok}: FAILED {type(err).__name__}: {err}")
        notify(
            f"⚠️ <b>Turtle/{html_escape(tok)} failed</b> and was skipped. "
            f"Other instruments still ran.\n"
            f"<code>{html_escape(type(err).__name__)}: {html_escape(err)}</code>\n"
            f"If a live order just filled, the next run will see a position this book "
            f"doesn't have and will stand down on {html_escape(tok)} rather than manage it."
        )

    failed = process_all(TOKENS, handle, on_error)
    if seeds:
        notify(
            f"Seeded the latest completed candle on {len(seeds)} instruments and "
            f"placed no orders for them. The next completed daily candle is the "
            f"first one this book will trade. "
            f"({', '.join(seeds)})"
        )
    if skips:
        notify("Skipped units that cannot meet the exchange minimum or the leverage cap:\n" + "\n".join(skips))

    equity_log.append({
        "ts": now, "equity": account_value, "equity_xyz": equity_account_value, "mode": mode,
    })
    save(equity_path, equity_log)
    persist()
    print(f"Done. mode={mode} seeded={len(seeds)} skipped={len(skips)} failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        traceback.print_exc()
        try:
            notify(
                "⚠️ <b>Turtle bot crashed</b> — this run aborted.\n"
                f"<code>{html_escape(type(e).__name__)}: {html_escape(e)}</code>"
            )
        except Exception:
            print("  could not send crash alert")
        sys.exit(1)
    sys.exit(code or 0)
