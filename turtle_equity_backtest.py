#!/usr/bin/env python3
"""
Backtests the new equity/macro shortlist (turtle_equity_lib.EQUITY_ROSTER),
standalone and blended with the existing 18-token crypto roster, using the
exact same simulate_portfolio engine as turtle_multiasset_backtest.py - no
reimplementation, just different token_data. System 2 only (System 1 was
unprofitable on the crypto roster at every leverage tested; no reason to
re-litigate that here).

Equity data only goes back to 2021-11 (Yahoo's 5y window at the time this
was run), vs crypto's 3y - the blended run is naturally bounded to the
shorter overlap.
"""
import json, os
from datetime import datetime, timezone

from turtle_backtest import compute_N
from turtle_multiasset_backtest import simulate_portfolio, report
from turtle_multiasset_fetch import TOKENS as CRYPTO_TOKENS, OUT_DIR as CRYPTO_DIR
from turtle_equity_lib import TOKENS as EQUITY_TOKENS, DATA_DIR as EQUITY_DIR, load_candles as load_equity


def load_crypto():
    data = {}
    for tok in CRYPTO_TOKENS:
        candles = json.load(open(os.path.join(CRYPTO_DIR, f"{tok}.json")))
        data[tok] = (candles, compute_N(candles))
    return data


def load_equity_data():
    data = {}
    for tok in EQUITY_TOKENS:
        candles = load_equity(tok)
        data[tok] = (candles, compute_N(candles))
    return data


def run(name, token_data):
    trades, eq = simulate_portfolio(token_data, entry_n=55, exit_n=20,
                                     use_skip_filter=False, leverage_cap=3,
                                     risk_capital_fraction=0.25)
    r = report(f"{name} - System 2 @ 3x cap / 25% risk capital", trades, eq)
    return trades, eq, r


def main():
    print("Loading data...")
    equity_data = load_equity_data()
    crypto_data = load_crypto()
    for tok, (candles, N) in equity_data.items():
        span = f"{datetime.fromtimestamp(candles[0]['ts'],tz=timezone.utc).date()} -> {datetime.fromtimestamp(candles[-1]['ts'],tz=timezone.utc).date()}" if candles else "no data"
        print(f"  {tok:<8} {len(candles):>5} bars  {span}")

    print("\n" + "=" * 78)
    print("STANDALONE: 10-instrument equity/macro shortlist")
    print("=" * 78)
    t_eq, e_eq, r_eq = run("Equity/macro shortlist (10 tokens)", equity_data)

    print("\n" + "=" * 78)
    print("STANDALONE: 18-token crypto roster (reference, same window math)")
    print("=" * 78)
    t_cr, e_cr, r_cr = run("Crypto roster (18 tokens)", crypto_data)

    print("\n" + "=" * 78)
    print("BLENDED: 28 tokens (18 crypto + 10 equity/macro)")
    print("=" * 78)
    blended = {**crypto_data, **equity_data}
    t_bl, e_bl, r_bl = run("Blended (28 tokens)", blended)

    json.dump({
        "equity_only": {"trades": t_eq, "equity_curve": e_eq, "summary": r_eq},
        "crypto_only_same_window": {"trades": t_cr, "equity_curve": e_cr, "summary": r_cr},
        "blended_28": {"trades": t_bl, "equity_curve": e_bl, "summary": r_bl},
    }, open("turtle_equity_backtest_results.json", "w"), indent=1)
    print("\nSaved to turtle_equity_backtest_results.json")


if __name__ == "__main__":
    main()
