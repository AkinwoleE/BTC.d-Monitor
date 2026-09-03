#!/usr/bin/env python3
"""
Turtle Trading System backtest on BTC, implemented directly from "The
Original Turtle Trading Rules" (originalturtles.org, Curtis Faith, 2003) -
the primary source, not a secondary summary. Key mechanics reproduced
faithfully:

  N (volatility): True Range = max(H-L, H-PDC, PDC-L); N = (19*PDN + TR)/20,
  seeded with a 20-day simple average of TR.

  Unit size: 1N of adverse price movement = 1% of account equity. For BTC
  spot (dollars-per-point = 1, unlike futures contract multipliers), this is
  simply Unit(BTC) = (0.01 * equity) / N.

  System 1: 20-day breakout entry, 10-day opposite-extreme exit, 2N stop.
  Skip filter: a new breakout is ignored if the *last breakout in this
  market* (taken or not) would have been a winner - defined per the docs as
  "price moved 2N against before a profitable 10-day exit." If skipped, a
  55-day breakout (the Failsafe) is still taken to avoid missing major moves.

  System 2: 55-day breakout entry, 20-day opposite-extreme exit, 2N stop,
  no skip filter, every breakout taken.

  Pyramiding: add 1 unit every 0.5N favorable move from the last unit's
  fill, up to 4 units. Stops for all units ratchet to 2N from the most
  recently added unit.

Single-market simplification: the real Turtles traded ~20 diversified
futures markets; this is BTC only, so the correlated-market position caps
don't apply - only the 4-units-per-single-market cap does, which is
implemented via the pyramid limit. Not implemented: the documented "cut
notional account 20% per 10% drawdown" money-management overlay - a
secondary refinement; equity here compounds continuously off realized P&L,
the standard simplification for single-market backtests of this system.
"""
import json
from datetime import datetime, timezone

RISK_PCT = 0.01          # 1% of equity per 1N -> 2% per 2N stop
DOLLARS_PER_POINT = 1    # spot BTC: $1 price move = $1 P&L per 1 BTC held
STARTING_EQUITY = 10000
MAX_UNITS = 4


def load_daily_btc(path="btcusdt_1h_binance_3y.json"):
    c1h = json.load(open(path))
    by_day = {}
    for c in c1h:
        day = c["ts"] - (c["ts"] % 86400)
        by_day.setdefault(day, []).append(c)
    days = sorted(by_day)
    daily = []
    for day in days[1:-1]:               # drop first/last: likely partial
        bars = sorted(by_day[day], key=lambda c: c["ts"])
        if len(bars) < 20:
            continue
        daily.append({"ts": day, "open": bars[0]["open"],
                       "high": max(b["high"] for b in bars),
                       "low": min(b["low"] for b in bars),
                       "close": bars[-1]["close"]})
    return daily


def compute_N(candles):
    n = len(candles)
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = candles[i]["high"] - candles[i]["low"]
        else:
            pdc = candles[i - 1]["close"]
            tr[i] = max(candles[i]["high"] - candles[i]["low"],
                        abs(candles[i]["high"] - pdc), abs(pdc - candles[i]["low"]))
    N = [None] * n
    if n < 20:
        return N
    N[19] = sum(tr[0:20]) / 20
    for i in range(20, n):
        N[i] = (19 * N[i - 1] + tr[i]) / 20
    return N


def hh(candles, i, n):
    return max(c["high"] for c in candles[i - n:i])


def ll(candles, i, n):
    return min(c["low"] for c in candles[i - n:i])


def classify_breakout(candles, N, start_i, direction):
    """Per the docs: a breakout (taken or not) is a 'losing breakout' if price
    moves 2N against it before a profitable 10-day-exit occurs; else a
    winner. Returns 'win'/'loss'/None (unresolved within available data)."""
    entry = candles[start_i]["high"] if direction == "long" else candles[start_i]["low"]
    n0 = N[start_i]
    if n0 is None:
        return None
    stop = entry - 2 * n0 if direction == "long" else entry + 2 * n0
    for j in range(start_i + 1, len(candles)):
        c = candles[j]
        if direction == "long" and c["low"] <= stop:
            return "loss"
        if direction == "short" and c["high"] >= stop:
            return "loss"
        exit_lvl = ll(candles, j, 10) if direction == "long" else hh(candles, j, 10)
        hit = (direction == "long" and c["low"] <= exit_lvl) or \
              (direction == "short" and c["high"] >= exit_lvl)
        if hit:
            pnl = (exit_lvl - entry) if direction == "long" else (entry - exit_lvl)
            return "win" if pnl > 0 else "loss"
    return None


def simulate(candles, N, entry_n, exit_n, use_skip_filter):
    equity = STARTING_EQUITY
    equity_curve = []
    units = []
    trades = []
    last_breakout_outcome = None
    warmup = max(entry_n, 55) + 1

    def stop_price():
        latest = units[-1]
        n = latest["n_at_entry"]
        return latest["entry_px"] - 2 * n if latest["side"] == "long" else latest["entry_px"] + 2 * n

    for i in range(warmup, len(candles)):
        c = candles[i]
        n = N[i]
        if n is None or n <= 0:
            equity_curve.append({"ts": c["ts"], "equity": equity})
            continue

        if units:
            side = units[0]["side"]
            stop = stop_price()
            stopped = (side == "long" and c["low"] <= stop) or (side == "short" and c["high"] >= stop)
            if side == "long":
                exit_lvl = ll(candles, i, exit_n)
                channel_hit = c["low"] <= exit_lvl
            else:
                exit_lvl = hh(candles, i, exit_n)
                channel_hit = c["high"] >= exit_lvl

            if stopped or channel_hit:
                exit_px = stop if stopped else exit_lvl
                pnl = sum((exit_px - u["entry_px"]) * u["sz"] if side == "long"
                          else (u["entry_px"] - exit_px) * u["sz"] for u in units)
                equity += pnl
                trades.append({"side": side, "units": len(units), "entry_ts": units[0]["entry_ts"],
                               "exit_ts": c["ts"], "pnl": round(pnl, 2),
                               "reason": "STOP" if stopped else "CHANNEL_EXIT"})
                units = []
            else:
                if len(units) < MAX_UNITS:
                    last_fill = units[-1]["entry_px"]
                    trigger = last_fill + 0.5 * n if side == "long" else last_fill - 0.5 * n
                    add = (side == "long" and c["high"] >= trigger) or (side == "short" and c["low"] <= trigger)
                    if add:
                        sz = (RISK_PCT * equity) / (n * DOLLARS_PER_POINT)
                        units.append({"side": side, "entry_px": trigger, "sz": sz,
                                     "n_at_entry": n, "entry_ts": c["ts"]})
            equity_curve.append({"ts": c["ts"], "equity": equity})
            continue

        hi_entry, lo_entry = hh(candles, i, entry_n), ll(candles, i, entry_n)
        long_sig, short_sig = c["high"] > hi_entry, c["low"] < lo_entry
        take_long = take_short = False
        entry_px = None

        if long_sig or short_sig:
            direction = "long" if long_sig else "short"   # rare same-day-both-signal case defaults long
            trigger_px = hi_entry if direction == "long" else lo_entry
            if use_skip_filter:
                outcome = classify_breakout(candles, N, i, direction)
                if outcome is not None:
                    last_breakout_outcome = outcome
                if last_breakout_outcome != "win":
                    take_long, take_short, entry_px = direction == "long", direction == "short", trigger_px
                else:
                    hi55, lo55 = hh(candles, i, 55), ll(candles, i, 55)
                    if c["high"] > hi55:
                        take_long, entry_px = True, hi55
                    elif c["low"] < lo55:
                        take_short, entry_px = True, lo55
            else:
                take_long, take_short, entry_px = direction == "long", direction == "short", trigger_px

        if take_long or take_short:
            side = "long" if take_long else "short"
            sz = (RISK_PCT * equity) / (n * DOLLARS_PER_POINT)
            units = [{"side": side, "entry_px": entry_px, "sz": sz, "n_at_entry": n, "entry_ts": c["ts"]}]

        equity_curve.append({"ts": c["ts"], "equity": equity})

    return trades, equity_curve


def report(name, trades, equity_curve, candles):
    final_eq = equity_curve[-1]["equity"] if equity_curve else STARTING_EQUITY
    total_ret = 100 * (final_eq / STARTING_EQUITY - 1)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    years = (candles[-1]["ts"] - candles[0]["ts"]) / (365.25 * 86400)
    cagr = (100 * ((final_eq / STARTING_EQUITY) ** (1 / years) - 1)) if years > 0 and final_eq > 0 else None

    peak, max_dd = STARTING_EQUITY, 0.0
    for s in equity_curve:
        peak = max(peak, s["equity"])
        max_dd = max(max_dd, 100 * (peak - s["equity"]) / peak) if peak > 0 else max_dd

    print(f"\n=== {name} ===")
    print(f"Trades: {len(trades)}  Wins: {len(wins)}  Losses: {len(losses)}  "
          f"Win rate: {100*len(wins)/len(trades):.1f}%" if trades else "No trades")
    if trades:
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        print(f"Avg win: ${avg_win:,.2f}  Avg loss: ${avg_loss:,.2f}  "
              f"Win/loss $ ratio: {abs(avg_win/avg_loss):.2f}" if avg_loss else "")
        by_reason = {}
        for t in trades:
            by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
        print(f"Exit reasons: {by_reason}")
        pyramided = sum(1 for t in trades if t["units"] > 1)
        print(f"Trades that pyramided beyond 1 unit: {pyramided} ({100*pyramided/len(trades):.1f}%)")
    print(f"Final equity: ${final_eq:,.2f}  Total return: {total_ret:+.1f}%  "
          f"CAGR: {cagr:+.1f}%" if cagr is not None else f"Final equity: ${final_eq:,.2f}")
    print(f"Max drawdown: {max_dd:.1f}%")
    return {"name": name, "trades": len(trades), "win_rate": round(100*len(wins)/len(trades),1) if trades else None,
            "final_equity": round(final_eq,2), "total_return_pct": round(total_ret,1),
            "cagr_pct": round(cagr,1) if cagr is not None else None, "max_dd_pct": round(max_dd,1)}


def main():
    candles = load_daily_btc()
    print(f"BTC daily bars: {len(candles)}  "
          f"span: {datetime.fromtimestamp(candles[0]['ts'],tz=timezone.utc).date()} -> "
          f"{datetime.fromtimestamp(candles[-1]['ts'],tz=timezone.utc).date()}")
    N = compute_N(candles)

    t1, eq1 = simulate(candles, N, entry_n=20, exit_n=10, use_skip_filter=True)
    r1 = report("System 1 (20-day entry / 10-day exit, skip filter + 55d failsafe)", t1, eq1, candles)

    t2, eq2 = simulate(candles, N, entry_n=55, exit_n=20, use_skip_filter=False)
    r2 = report("System 2 (55-day entry / 20-day exit, no filter)", t2, eq2, candles)

    # simple 50/50 blend for reference: each system trades half the capital independently
    blend_final = STARTING_EQUITY/2 * (eq1[-1]["equity"]/STARTING_EQUITY) + \
                  STARTING_EQUITY/2 * (eq2[-1]["equity"]/STARTING_EQUITY)
    print(f"\n=== 50/50 Blend (reference only) ===")
    print(f"Final equity: ${blend_final:,.2f}  Total return: {100*(blend_final/STARTING_EQUITY-1):+.1f}%")

    json.dump({"system1": {"trades": t1, "equity_curve": eq1, "summary": r1},
               "system2": {"trades": t2, "equity_curve": eq2, "summary": r2},
               "blend_final_equity": round(blend_final, 2)},
              open("turtle_backtest_results.json", "w"), indent=1)
    print("\nSaved full results to turtle_backtest_results.json")


if __name__ == "__main__":
    main()
