#!/usr/bin/env python3
"""
Fetch ~3 years of native daily (1d) Binance klines for the multi-asset Turtle
roster (18 tokens: on Decibel with >=5x max leverage AND a verified,
collision-free Binance USDT pair - see conversation for the verification).
Mirrors divergence100_lib.py's zip-fetch pattern but at 1d resolution, kept
separate to avoid touching that already-scheduled pipeline.
"""
import io, json, os, time, zipfile, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TOKENS = ["AAVE","ADA","APT","AVAX","BNB","BTC","DOGE","ETH","LINK","NEAR",
          "SOL","SUI","TAO","TRUMP","TRX","WLFI","XRP","ZEC"]
DECIBEL_MAX_LEV = {"BTC":40,"ETH":25,"SOL":20,"XRP":20,"ADA":10,"DOGE":10,"LINK":10,
                    "AVAX":10,"BNB":10,"TRX":10,"NEAR":5,"SUI":5,"AAVE":10,"TAO":5,
                    "ZEC":10,"WLFI":5,"TRUMP":5,"APT":10}

MONTHLY_URL = "https://data.binance.vision/data/spot/monthly/klines/{sym}/1d/{sym}-1d-{ym}.zip"
DAILY_URL   = "https://data.binance.vision/data/spot/daily/klines/{sym}/1d/{sym}-1d-{d}.zip"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "turtle_multiasset")
os.makedirs(OUT_DIR, exist_ok=True)


def fetch_zip_csv(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
            zf = zipfile.ZipFile(io.BytesIO(raw))
            return zf.read(zf.namelist()[0]).decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1.5 * (i + 1))
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def parse_klines_csv(text):
    out = []
    for line in text.strip().split("\n"):
        parts = line.split(",")
        if not parts[0].lstrip("-").isdigit():
            continue
        ts_raw = int(parts[0])
        if ts_raw > 10 ** 14:
            ts_raw //= 1000
        out.append({"ts": ts_raw // 1000, "open": float(parts[1]), "high": float(parts[2]),
                     "low": float(parts[3]), "close": float(parts[4])})
    return out


def month_range(start_dt, end_dt):
    y, m = start_dt.year, start_dt.month
    out = []
    while (y, m) <= (end_dt.year, end_dt.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def backfill_tasks(symbol, start_dt, end_dt):
    tasks = [(symbol, MONTHLY_URL.format(sym=symbol, ym=ym)) for ym in month_range(start_dt, end_dt)]
    d = end_dt.replace(day=1)
    while d <= end_dt:
        tasks.append((symbol, DAILY_URL.format(sym=symbol, d=d.strftime("%Y-%m-%d"))))
        d += timedelta(days=1)
    return tasks


def main():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=365 * 3)
    all_tasks = []
    for tok in TOKENS:
        all_tasks.extend(backfill_tasks(tok + "USDT", start_dt, end_dt))
    print(f"Fetching {len(all_tasks)} daily-kline archive files for {len(TOKENS)} tokens...")

    by_symbol = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(fetch_zip_csv, url): sym for sym, url in all_tasks}
        done = 0
        for fut in as_completed(futs):
            sym = futs[fut]
            text = fut.result()
            done += 1
            if text:
                by_symbol.setdefault(sym, []).extend(parse_klines_csv(text))
            if done % 100 == 0:
                print(f"  ...{done}/{len(all_tasks)}")

    now_ts = int(end_dt.timestamp())
    for tok in TOKENS:
        sym = tok + "USDT"
        candles = by_symbol.get(sym, [])
        seen = set()
        deduped = []
        for c in sorted(candles, key=lambda c: c["ts"]):
            if c["ts"] in seen or c["ts"] + 86400 > now_ts:
                continue
            seen.add(c["ts"])
            deduped.append(c)
        json.dump(deduped, open(os.path.join(OUT_DIR, f"{tok}.json"), "w"))
        span = f"{deduped[0]['ts']}-{deduped[-1]['ts']}" if deduped else "NO DATA"
        print(f"  {tok:<6} {len(deduped):>5} daily bars")

    print("Done.")


if __name__ == "__main__":
    main()
