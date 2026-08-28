#!/usr/bin/env python3
"""
Shared helpers for the Divergence 100 pipeline (build + daily update).

Detection/stat logic is reused as-is from divergence_monitor.py (rsi, pivots,
find_divergences, update_forward, compute_stats) so every token is scored with
the exact same RSI-14 / k=3-pivot methodology already validated for BTC.
Only the data source differs: native Binance 2h klines (see NOTE below).

NOTE on candle resolution: BTC's original backtest paired Kraken 1h candles
into synthetic 2h bars (resample_2h) and forward-tracked at 1h resolution for
precise best-entry/exit timing. Here we use Binance's *native* 2h klines
directly for both detection and forward-tracking (verified: Binance 2h open
times are UTC-epoch-aligned every 7200s, same boundary convention as
resample_2h) — 6x less data across 50 tokens x 3 years, and per-token timing
precision isn't a feature of this leaderboard (no per-episode detail view).
"""
import io, json, os, time, urllib.request, urllib.error, zipfile
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from divergence_monitor import rsi, pivots, find_divergences, update_forward, compute_stats  # noqa: F401

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "divergence100")
os.makedirs(DATA_DIR, exist_ok=True)

INTERVAL = "2h"
MONTHLY_URL = "https://data.binance.vision/data/spot/monthly/klines/{sym}/2h/{sym}-2h-{ym}.zip"
DAILY_URL   = "https://data.binance.vision/data/spot/daily/klines/{sym}/2h/{sym}-2h-{d}.zip"
# data-api.binance.vision, NOT api.binance.com: Binance's documented public
# "market data only" host — api.binance.com returns 451 from GitHub Actions'
# IP ranges (geo-blocked as a restricted-location trading API), even for
# read-only GET endpoints. This mirror serves the same public klines/ticker
# data with no such restriction (discovered 2026-08-29 when the live
# workflow run failed; local testing never caught it since dev IPs aren't
# blocked).
LIVE_KLINES = "https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=2h&limit={limit}"

def token_file(symbol):
    return os.path.join(DATA_DIR, f"{symbol}.json")

def _http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def fetch_zip_csv(url, retries=3):
    for i in range(retries):
        try:
            raw = _http_get(url)
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
            continue  # header row, if ever present
        ts_raw = int(parts[0])
        if ts_raw > 10 ** 14:      # 2026 monthly-export microsecond gotcha
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
    """(symbol, url) pairs covering [start_dt, end_dt]: monthly zips for every
    month in range (older ones exist, current month 404s and is skipped), plus
    daily zips for every day of the current month (covers the gap)."""
    tasks = [(symbol, MONTHLY_URL.format(sym=symbol, ym=ym)) for ym in month_range(start_dt, end_dt)]
    d = end_dt.replace(day=1)
    while d <= end_dt:
        tasks.append((symbol, DAILY_URL.format(sym=symbol, d=d.strftime("%Y-%m-%d"))))
        d += timedelta(days=1)
    return tasks

def fetch_many(tasks, max_workers=20):
    """Run (symbol, url) fetch tasks concurrently. Returns {symbol: [candles...]}."""
    by_symbol = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_zip_csv, url): sym for sym, url in tasks}
        done = 0
        for fut in as_completed(futs):
            sym = futs[fut]
            text = fut.result()
            done += 1
            if text:
                by_symbol.setdefault(sym, []).extend(parse_klines_csv(text))
            if done % 200 == 0:
                print(f"  ...{done}/{len(tasks)} fetches done")
    for sym in by_symbol:
        seen = set()
        deduped = []
        for c in sorted(by_symbol[sym], key=lambda c: c["ts"]):
            if c["ts"] in seen:
                continue
            seen.add(c["ts"])
            deduped.append(c)
        by_symbol[sym] = deduped
    return by_symbol

def fetch_live_incremental(symbol, since_ts, limit=1000):
    """Binance live REST 2h klines strictly after since_ts (for the daily updater)."""
    raw = _http_get(LIVE_KLINES.format(sym=symbol, limit=limit))
    rows = json.loads(raw)
    out = []
    for r in rows:
        ts = r[0] // 1000
        if ts > since_ts:
            out.append({"ts": ts, "open": float(r[1]), "high": float(r[2]),
                        "low": float(r[3]), "close": float(r[4])})
    return out

def load_candles(symbol):
    """Storage format is compact [ts,open,high,low,close] rows to keep 50
    tokens x 3 years of history from bloating the repo; expand to the
    dict-per-candle shape divergence_monitor's functions expect."""
    try:
        rows = json.load(open(token_file(symbol)))["candles"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return []
    return [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows]

def save_candles(symbol, candles):
    compact = [[c["ts"], c["open"], c["high"], c["low"], c["close"]] for c in candles]
    json.dump({"symbol": symbol, "updated": datetime.now(timezone.utc).isoformat(),
                "candles": compact}, open(token_file(symbol), "w"))

def build_episode_stats(candles, now_ts=None):
    """Run detection + forward-tracking on a full closed 2h candle history and
    return (episodes, agg_stats). `now_ts` lets a live run drop candles that
    haven't closed yet; omit for a pure historical backfill."""
    if now_ts is not None:
        candles = [c for c in candles if c["ts"] + 7200 <= now_ts]
    if len(candles) < 60:
        return [], None
    rs = rsi([c["close"] for c in candles])
    eps = find_divergences(candles, rs, "2h")
    for ep in eps:
        ep["forward"] = []
        update_forward(ep, candles)
    done = [e for e in eps if e.get("stats", {}).get("ret_24h") is not None]
    if not done:
        return eps, {"episodes": len(eps), "done": 0, "win_rate": None,
                      "avg_ret_24h": None, "total_ret_24h": None,
                      "n3": sum(1 for e in eps if e.get("legs", 3) == 3),
                      "n2": sum(1 for e in eps if e.get("legs", 3) == 2)}
    wins = sum(1 for e in done if e["stats"]["ret_24h"] > 0)
    total = sum(e["stats"]["ret_24h"] for e in done)
    agg = {
        "episodes": len(eps), "done": len(done),
        "win_rate": round(100 * wins / len(done), 1),
        "avg_ret_24h": round(total / len(done), 3),
        "total_ret_24h": round(total, 3),
        "n3": sum(1 for e in eps if e.get("legs", 3) == 3),
        "n2": sum(1 for e in eps if e.get("legs", 3) == 2),
        "bear": sum(1 for e in eps if e["direction"] == "bearish"),
        "bull": sum(1 for e in eps if e["direction"] == "bullish"),
    }
    return eps, agg
