#!/usr/bin/env python3
"""
Shared helpers for the live multi-asset Turtle trader on Hyperliquid
(turtle_hyperliquid_trader.py): daily candle fetch/persistence from Binance
(signal source) - separate from execution, which happens on Hyperliquid.

Uses data-api.binance.vision, not api.binance.com - the latter 451s from
GitHub Actions' IP ranges even on public GET endpoints (discovered building
token_alerts.py). Same fix applied here proactively.
"""
import json, os, time, urllib.request

from turtle_multiasset_fetch import TOKENS, DECIBEL_MAX_LEV  # noqa: F401 (re-exported)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data", "turtle_hyperliquid")
os.makedirs(DATA_DIR, exist_ok=True)

LIVE_DAILY = "https://data-api.binance.vision/api/v3/klines?symbol={sym}USDT&interval=1d&limit={limit}"


def token_file(tok):
    return os.path.join(DATA_DIR, f"{tok}.json")


def load_candles(tok):
    try:
        return json.load(open(token_file(tok)))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_candles(tok, candles):
    json.dump(candles, open(token_file(tok), "w"))


def fetch_live_daily(tok, limit=1000):
    req = urllib.request.Request(LIVE_DAILY.format(sym=tok, limit=limit), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rows = json.loads(r.read())
    return [{"ts": row[0] // 1000, "open": float(row[1]), "high": float(row[2]),
             "low": float(row[3]), "close": float(row[4])} for row in rows]


def update_all(now_ts=None):
    """Refresh every token's persisted daily history with newly-closed candles.
    Returns {tok: candles}."""
    now_ts = now_ts or int(time.time())
    out = {}
    for tok in TOKENS:
        existing = load_candles(tok)
        fresh = fetch_live_daily(tok)
        have = {c["ts"] for c in existing}
        merged = existing + [c for c in fresh if c["ts"] not in have]
        merged = sorted(merged, key=lambda c: c["ts"])
        merged = [c for c in merged if c["ts"] + 86400 <= now_ts]   # drop still-forming candle
        save_candles(tok, merged)
        out[tok] = merged
        print(f"  {tok:<6} {len(merged):>5} daily bars (updated)")
    return out
