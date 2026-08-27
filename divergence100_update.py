#!/usr/bin/env python3
"""
Daily incremental refresh for Divergence 100 (see divergence100_build.py for
the one-time full backfill). Cheap by design: pulls only new closed 2h klines
since each token's last stored candle via Binance's live REST endpoint (no
bulk-archive re-download), recomputes stats from the full stored history, and
rewrites divergence100.json.

The 50-token roster (divergence100_roster.json) is fixed at build time —
this script refreshes price history/stats, not top-100 membership. Re-run
the CoinGecko matching step (see project notes) periodically if the roster
should track market-cap-rank churn.
"""
import json, time
from datetime import datetime, timezone

from divergence100_lib import fetch_live_incremental, load_candles, save_candles, build_episode_stats

ROSTER = "divergence100_roster.json"
OUT = "divergence100.json"

def main():
    roster = json.load(open(ROSTER))
    now_ts = int(time.time())
    rows, missing = [], []

    for tok in roster["tokens"]:
        sym = tok["symbol"] + "USDT"
        candles = load_candles(sym)
        if candles:
            new = fetch_live_incremental(sym, since_ts=candles[-1]["ts"])
            if new:
                have = {c["ts"] for c in candles}
                candles.extend(c for c in new if c["ts"] not in have)
                candles.sort(key=lambda c: c["ts"])
        else:
            # no prior backfill for this token (e.g. added after the last full
            # build) — pull what the live endpoint allows (up to 1000 candles)
            candles = fetch_live_incremental(sym, since_ts=0, limit=1000)

        candles = [c for c in candles if c["ts"] + 7200 <= now_ts]
        if not candles:
            missing.append(tok["symbol"])
            print(f"  {tok['symbol']:<8} NO DATA")
            continue
        save_candles(sym, candles)

        eps, agg = build_episode_stats(candles)
        if agg is None:
            print(f"  {tok['symbol']:<8} {len(candles):>6} candles — too short, skipped")
            continue
        rows.append({"rank": tok["rank"], "symbol": tok["symbol"], "cg_id": tok["id"],
                     "candles": len(candles),
                     "history_days": round((candles[-1]["ts"] - candles[0]["ts"]) / 86400),
                     **agg})
        print(f"  {tok['symbol']:<8} {len(candles):>6} candles  eps={agg['episodes']:<3} avg24h={agg['avg_ret_24h']}")

    ranked = sorted([r for r in rows if r["avg_ret_24h"] is not None],
                     key=lambda r: r["avg_ret_24h"], reverse=True)
    unranked = [r for r in rows if r["avg_ret_24h"] is None]

    out = {"updated": datetime.now(timezone.utc).isoformat(),
           "universe": roster["universe"],
           "methodology": "RSI-14 (k=3 confirmed pivots), native Binance 2h klines, same detection as the BTC Divergence dashboard",
           "tokens": ranked + unranked,
           "excluded_stablecoins": roster["excluded_stablecoins"],
           "excluded_no_binance_pair": roster["excluded_no_binance_pair"] + [{"rank": None, "symbol": s} for s in missing]}
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"\nWrote {OUT}: {len(ranked)} ranked, {len(unranked)} unranked, {len(missing)} missing data")

if __name__ == "__main__":
    main()
