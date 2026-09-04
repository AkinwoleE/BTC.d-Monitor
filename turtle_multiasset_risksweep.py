#!/usr/bin/env python3
"""Sweep risk_capital_fraction to test whether allocating only a portion of
equity to the position-sizing/leverage basis (leaving the rest as an
untouched buffer) fixes the multi-asset survivability problem."""
from turtle_multiasset_backtest import load_all, simulate_portfolio, report

token_data = load_all()

print(f"{'System':<10}{'RiskFrac':<10}{'Trades':<8}{'WinRate':<9}{'TotalRet':<13}{'CAGR':<10}{'MaxDD':<8}")
for sys_name, params in [("System 1", dict(entry_n=20, exit_n=10, use_skip_filter=True)),
                           ("System 2", dict(entry_n=55, exit_n=20, use_skip_filter=False))]:
    for frac in [1.0, 0.5, 0.25, 0.1, 0.05]:
        trades, eq = simulate_portfolio(token_data, leverage_cap=3, risk_capital_fraction=frac, **params)
        r = report(f"{sys_name} risk_frac={frac}", trades, eq, verbose=False)
        wr = f"{r['win_rate']}%" if r['win_rate'] is not None else "-"
        cagr_s = f"{r['cagr_pct']:+.1f}%" if r['cagr_pct'] is not None else "-"
        print(f"{sys_name:<10}{frac:<10}{r['trades']:<8}{wr:<9}{r['total_return_pct']:+.1f}%{'':<6}{cagr_s:<10}{r['max_dd_pct']:.1f}%")
