#!/usr/bin/env python3
"""
One-time (re-runnable) full backfill for Divergence 100: pulls ~3 years of
native Binance 2h klines for every token in token roster, runs divergence
detection, persists per-token candle history + writes divergence100.json.

Usage: python3 divergence100_build.py roster.json
  roster.json: [{"rank": <cg market cap rank>, "symbol": "BTC", "id": "bitcoin"}, ...]
"""
import json, sys, time
from datetime import datetime, timedelta, timezone

from divergence100_lib import (backfill_tasks, fetch_many, save_candles,
                                 build_episode_stats, DATA_DIR)

OUT = "divergence100.json"
YEARS_BACK = 3

def main():
    roster = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "final_token_list.json"))
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=365 * YEARS_BACK)

    print(f"Building task list for {len(roster)} tokens, {start_dt.date()} -> {end_dt.date()}")
    all_tasks = []
    for tok in roster:
        sym = tok["symbol"] + "USDT"
        all_tasks.extend(backfill_tasks(sym, start_dt, end_dt))
    print(f"Total fetch tasks: {len(all_tasks)}")

    t0 = time.time()
    by_symbol = fetch_many(all_tasks, max_workers=24)
    print(f"Fetched candles for {len(by_symbol)}/{len(roster)} symbols in {time.time()-t0:.0f}s")

    now_ts = int(end_dt.timestamp())
    rows = []
    for tok in roster:
        sym = tok["symbol"] + "USDT"
        candles = by_symbol.get(sym, [])
        candles = [c for c in candles if c["ts"] + 7200 <= now_ts]
        if not candles:
            print(f"  {tok['symbol']:<8} NO DATA (0 candles) — skipping")
            continue
        save_candles(sym, candles)
        eps, agg = build_episode_stats(candles)
        span_days = (candles[-1]["ts"] - candles[0]["ts"]) / 86400
        if agg is None:
            print(f"  {tok['symbol']:<8} {len(candles):>6} candles ({span_days:.0f}d) — too short, skipped")
            continue
        row = {"rank": tok["rank"], "symbol": tok["symbol"], "cg_id": tok["id"],
               "candles": len(candles), "history_days": round(span_days), **agg}
        rows.append(row)
        print(f"  {tok['symbol']:<8} {len(candles):>6} candles ({span_days:>4.0f}d) "
              f"eps={agg['episodes']:<3} avg24h={agg['avg_ret_24h']}")

    ranked = sorted([r for r in rows if r["avg_ret_24h"] is not None],
                     key=lambda r: r["avg_ret_24h"], reverse=True)
    unranked = [r for r in rows if r["avg_ret_24h"] is None]
    missing = [t["symbol"] for t in roster if (t["symbol"] + "USDT") not in by_symbol
               or not by_symbol.get(t["symbol"] + "USDT")]

    out = {"updated": datetime.now(timezone.utc).isoformat(),
           "tokens": ranked + unranked, "excluded_no_data": missing}
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"\nWrote {OUT}: {len(ranked)} ranked, {len(unranked)} unranked (no complete episodes), "
          f"{len(missing)} with no data at all")

if __name__ == "__main__":
    main()
