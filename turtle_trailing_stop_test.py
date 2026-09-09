#!/usr/bin/env python3
"""
Tests whether a trailing stop improves on the current design (2N stop that
only ratchets on pyramid adds + 20-day channel exit) for Turtle System 2 on
the same 18-token portfolio backtest that informed the live dry-run config.

Three questions, tested empirically rather than argued in the abstract:
  1. Baseline: current design, unchanged.
  2. Trailing stop as an ADDITIONAL protective layer on top of the channel
     exit (whichever is tighter wins) - directly tests the "protect gains
     from round-tripping before the slow channel exit catches it" concern.
  3. Trailing stop REPLACING the channel exit entirely - tests whether a
     pure trailing-stop trend-follower beats the original design's
     "wide channel, let it breathe" philosophy.

Distances tested: 2N and 3N trailing (the current stop is also 2N-based,
so 2N trailing is the closest apples-to-apples comparison; 3N is a wider
middle ground).
"""
import json

from turtle_backtest import compute_N, hh, ll, classify_breakout, RISK_PCT, DOLLARS_PER_POINT, MAX_UNITS, STARTING_EQUITY
from turtle_multiasset_backtest import load_all, report


def simulate_portfolio_v2(token_data, entry_n, exit_n, use_skip_filter, leverage_cap,
                           risk_capital_fraction=1.0, trailing_n=None, disable_channel_exit=False):
    equity = STARTING_EQUITY
    units = {tok: [] for tok in token_data}
    peak = {tok: None for tok in token_data}
    last_outcome = {tok: None for tok in token_data}
    trades = []
    equity_curve = []

    idx = {tok: {c["ts"]: i for i, c in enumerate(candles)} for tok, (candles, N) in token_data.items()}
    warmup_i = max(entry_n, 55) + 1
    all_dates = sorted(set(c["ts"] for candles, N in token_data.values() for c in candles))

    def current_price(tok, ts):
        i = idx[tok].get(ts)
        return token_data[tok][0][i]["close"] if i is not None else None

    def notional_of(tok, ts):
        if not units[tok]:
            return 0
        px = current_price(tok, ts) or units[tok][-1]["entry_px"]
        return sum(u["sz"] for u in units[tok]) * px

    def total_notional(ts):
        return sum(notional_of(t, ts) for t in token_data)

    for ts in all_dates:
        for tok, (candles, N) in token_data.items():
            i = idx[tok].get(ts)
            if i is None or i < warmup_i or not units[tok]:
                continue
            c, n = candles[i], N[i]
            if n is None or n <= 0:
                continue
            side = units[tok][0]["side"]
            latest = units[tok][-1]
            base_stop = latest["entry_px"] - 2 * latest["n_at_entry"] if side == "long" else latest["entry_px"] + 2 * latest["n_at_entry"]

            stop = base_stop
            if trailing_n is not None:
                if side == "long":
                    peak[tok] = c["high"] if peak[tok] is None else max(peak[tok], c["high"])
                    stop = max(base_stop, peak[tok] - trailing_n * n)
                else:
                    peak[tok] = c["low"] if peak[tok] is None else min(peak[tok], c["low"])
                    stop = min(base_stop, peak[tok] + trailing_n * n)

            stopped = (side == "long" and c["low"] <= stop) or (side == "short" and c["high"] >= stop)
            channel_hit = False
            if not disable_channel_exit:
                if side == "long":
                    exit_lvl = ll(candles, i, exit_n); channel_hit = c["low"] <= exit_lvl
                else:
                    exit_lvl = hh(candles, i, exit_n); channel_hit = c["high"] >= exit_lvl

            if stopped or channel_hit:
                exit_px = stop if stopped else exit_lvl
                pnl = sum((exit_px - u["entry_px"]) * u["sz"] if side == "long"
                          else (u["entry_px"] - exit_px) * u["sz"] for u in units[tok])
                equity += pnl
                trades.append({"token": tok, "side": side, "units": len(units[tok]),
                               "entry_ts": units[tok][0]["entry_ts"], "exit_ts": ts,
                               "pnl": round(pnl, 2), "reason": "STOP" if stopped else "CHANNEL_EXIT"})
                units[tok] = []
                peak[tok] = None
            elif len(units[tok]) < MAX_UNITS:
                last_fill = latest["entry_px"]
                trigger = last_fill + 0.5 * n if side == "long" else last_fill - 0.5 * n
                add = (side == "long" and c["high"] >= trigger) or (side == "short" and c["low"] <= trigger)
                if add:
                    risk_capital = risk_capital_fraction * equity
                    sz = (RISK_PCT * risk_capital) / (n * DOLLARS_PER_POINT)
                    prospective = total_notional(ts) + sz * trigger
                    if leverage_cap is None or prospective <= leverage_cap * risk_capital:
                        units[tok].append({"side": side, "entry_px": trigger, "sz": sz,
                                           "n_at_entry": n, "entry_ts": ts})

        for tok, (candles, N) in token_data.items():
            i = idx[tok].get(ts)
            if i is None or i < warmup_i or units[tok]:
                continue
            c, n = candles[i], N[i]
            if n is None or n <= 0:
                continue
            hi_entry, lo_entry = hh(candles, i, entry_n), ll(candles, i, entry_n)
            long_sig, short_sig = c["high"] > hi_entry, c["low"] < lo_entry
            take_long = take_short = False
            entry_px = None
            if long_sig or short_sig:
                direction = "long" if long_sig else "short"
                trigger_px = hi_entry if direction == "long" else lo_entry
                if use_skip_filter:
                    outcome = classify_breakout(candles, N, i, direction)
                    if outcome is not None:
                        last_outcome[tok] = outcome
                    if last_outcome[tok] != "win":
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
                risk_capital = risk_capital_fraction * equity
                sz = (RISK_PCT * risk_capital) / (n * DOLLARS_PER_POINT)
                prospective = total_notional(ts) + sz * entry_px
                if leverage_cap is None or prospective <= leverage_cap * risk_capital:
                    units[tok] = [{"side": side, "entry_px": entry_px, "sz": sz, "n_at_entry": n, "entry_ts": ts}]
                    peak[tok] = entry_px

        equity_curve.append({"ts": ts, "equity": equity})

    return trades, equity_curve


def main():
    token_data = load_all()
    params = dict(entry_n=55, exit_n=20, use_skip_filter=False, leverage_cap=3, risk_capital_fraction=0.25)

    configs = [
        ("Baseline (current live design)", dict(trailing_n=None, disable_channel_exit=False)),
        ("Trailing 2N + channel exit (tighter wins)", dict(trailing_n=2, disable_channel_exit=False)),
        ("Trailing 3N + channel exit (tighter wins)", dict(trailing_n=3, disable_channel_exit=False)),
        ("Trailing 2N REPLACES channel exit", dict(trailing_n=2, disable_channel_exit=True)),
        ("Trailing 3N REPLACES channel exit", dict(trailing_n=3, disable_channel_exit=True)),
    ]

    print(f"{'Config':<45}{'Trades':<8}{'WinRate':<9}{'TotalRet':<12}{'CAGR':<10}{'MaxDD':<8}")
    results = {}
    for name, extra in configs:
        trades, eq = simulate_portfolio_v2(token_data, **params, **extra)
        r = report(name, trades, eq, verbose=False)
        results[name] = {"trades": trades, "equity_curve": eq, "summary": r}
        wr = f"{r['win_rate']}%" if r['win_rate'] is not None else "-"
        cagr_s = f"{r['cagr_pct']:+.1f}%" if r['cagr_pct'] is not None else "-"
        print(f"{name:<45}{r['trades']:<8}{wr:<9}{r['total_return_pct']:+.1f}%{'':<5}{cagr_s:<10}{r['max_dd_pct']:.1f}%")

    json.dump(results, open("turtle_trailing_stop_results.json", "w"), indent=1)
    print("\nSaved to turtle_trailing_stop_results.json")


if __name__ == "__main__":
    main()
