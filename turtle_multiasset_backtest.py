#!/usr/bin/env python3
"""
Portfolio-level multi-asset Turtle backtest across the 18-token Decibel/
Binance-verified roster (see turtle_multiasset_fetch.py). One shared equity
pool across all markets - not 18 independent single-asset runs - because
that's what "trading at 3x leverage" means for an actual account: the
leverage cap is enforced on TOTAL notional across all open positions, not
per-market. Per-market state (open units, the System 1 skip-filter's "last
breakout in that market") is still tracked independently per token, exactly
as the original rules specify.

Reuses compute_N/hh/ll/classify_breakout from turtle_backtest.py unchanged -
those are already generic over any candle series.
"""
import json, os
from datetime import datetime, timezone

from turtle_backtest import compute_N, hh, ll, classify_breakout, RISK_PCT, DOLLARS_PER_POINT, MAX_UNITS, STARTING_EQUITY
from turtle_multiasset_fetch import TOKENS, OUT_DIR, DECIBEL_MAX_LEV


def load_all():
    data = {}
    for tok in TOKENS:
        candles = json.load(open(os.path.join(OUT_DIR, f"{tok}.json")))
        N = compute_N(candles)
        data[tok] = (candles, N)
    return data


def simulate_portfolio(token_data, entry_n, exit_n, use_skip_filter, leverage_cap, risk_capital_fraction=1.0):
    """risk_capital_fraction: only this fraction of total equity is ever used
    as the sizing/leverage-cap basis (e.g. 0.4 = "$2000 at risk of $5000
    total") - the rest sits untouched as a buffer, never invested. equity
    itself (P&L, reporting, drawdown) always tracks the TRUE total account."""
    equity = STARTING_EQUITY
    units = {tok: [] for tok in token_data}
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
        # 1) manage existing positions: stops / channel exits / pyramid adds
        for tok, (candles, N) in token_data.items():
            i = idx[tok].get(ts)
            if i is None or i < warmup_i or not units[tok]:
                continue
            c, n = candles[i], N[i]
            if n is None or n <= 0:
                continue
            side = units[tok][0]["side"]
            latest = units[tok][-1]
            stop = latest["entry_px"] - 2 * latest["n_at_entry"] if side == "long" else latest["entry_px"] + 2 * latest["n_at_entry"]
            stopped = (side == "long" and c["low"] <= stop) or (side == "short" and c["high"] >= stop)
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

        # 2) look for new entries on flat tokens
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

        equity_curve.append({"ts": ts, "equity": equity})

    return trades, equity_curve


def report(name, trades, equity_curve, verbose=True):
    final_eq = equity_curve[-1]["equity"] if equity_curve else STARTING_EQUITY
    total_ret = 100 * (final_eq / STARTING_EQUITY - 1)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    years = (equity_curve[-1]["ts"] - equity_curve[0]["ts"]) / (365.25 * 86400) if equity_curve else 0
    cagr = (100 * ((final_eq / STARTING_EQUITY) ** (1 / years) - 1)) if years > 0 and final_eq > 0 else None

    peak, max_dd = STARTING_EQUITY, 0.0
    for s in equity_curve:
        peak = max(peak, s["equity"])
        max_dd = max(max_dd, 100 * (peak - s["equity"]) / peak) if peak > 0 else max_dd

    by_token = {}
    for t in trades:
        by_token.setdefault(t["token"], {"n": 0, "pnl": 0.0, "wins": 0})
        by_token[t["token"]]["n"] += 1
        by_token[t["token"]]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_token[t["token"]]["wins"] += 1

    if verbose:
        print(f"\n=== {name} ===")
        print(f"Trades: {len(trades)}  Win rate: {100*len(wins)/len(trades):.1f}%" if trades else "No trades")
        print(f"Final equity: ${final_eq:,.2f}  Total return: {total_ret:+.1f}%  "
              f"CAGR: {cagr:+.1f}%" if cagr is not None else f"Final equity: ${final_eq:,.2f}")
        print(f"Max drawdown: {max_dd:.1f}%")
        print(f"\n{'Token':<8}{'Trades':<8}{'WinRate':<9}{'Total PnL':<12}")
        for tok in sorted(by_token, key=lambda t: -by_token[t]["pnl"]):
            r = by_token[tok]
            print(f"{tok:<8}{r['n']:<8}{100*r['wins']/r['n']:.0f}%{'':<6}${r['pnl']:>+9,.2f}")

    return {"name": name, "trades": len(trades), "win_rate": round(100*len(wins)/len(trades),1) if trades else None,
            "final_equity": round(final_eq,2), "total_return_pct": round(total_ret,1),
            "cagr_pct": round(cagr,1) if cagr is not None else None, "max_dd_pct": round(max_dd,1),
            "by_token": {k: {"trades": v["n"], "pnl": round(v["pnl"],2),
                              "win_rate": round(100*v["wins"]/v["n"],1)} for k,v in by_token.items()}}


def main():
    print("Loading 18-token daily data...")
    token_data = load_all()
    for tok, (candles, N) in token_data.items():
        span = f"{datetime.fromtimestamp(candles[0]['ts'],tz=timezone.utc).date()} -> {datetime.fromtimestamp(candles[-1]['ts'],tz=timezone.utc).date()}"
        print(f"  {tok:<6} {len(candles):>5} bars  {span}  Decibel max lev: {DECIBEL_MAX_LEV[tok]}x")

    results = {}
    for cap_label, cap in [("3x", 3), ("uncapped", None)]:
        for sys_name, params in [("System 1", dict(entry_n=20, exit_n=10, use_skip_filter=True)),
                                   ("System 2", dict(entry_n=55, exit_n=20, use_skip_filter=False))]:
            trades, eq = simulate_portfolio(token_data, leverage_cap=cap, **params)
            r = report(f"{sys_name} @ {cap_label} portfolio leverage cap", trades, eq)
            results[f"{sys_name}_{cap_label}"] = {"trades": trades, "equity_curve": eq, "summary": r}

    print(f"\n\n{'='*78}\nSUMMARY\n{'='*78}")
    print(f"{'Config':<28}{'Trades':<8}{'WinRate':<9}{'TotalRet':<12}{'CAGR':<9}{'MaxDD':<8}")
    for key, res in results.items():
        r = res["summary"]
        wr = f"{r['win_rate']}%" if r['win_rate'] is not None else "-"
        cagr_s = f"{r['cagr_pct']:+.1f}%" if r['cagr_pct'] is not None else "-"
        print(f"{key:<28}{r['trades']:<8}{wr:<9}{r['total_return_pct']:+.1f}%{'':<5}{cagr_s:<9}{r['max_dd_pct']:.1f}%")

    json.dump(results, open("turtle_multiasset_results.json", "w"), indent=1)
    print("\nSaved to turtle_multiasset_results.json")


if __name__ == "__main__":
    main()
