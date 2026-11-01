#!/usr/bin/env python3
"""
Shared helpers for the equity/macro leg of the multi-asset Turtle trader:
daily candle fetch from Yahoo Finance (signal source) - separate from
execution, which happens on Hyperliquid's "xyz" HIP-3 builder dex (see
turtle_hyperliquid_trader.py). Mirrors turtle_hyperliquid_lib.py's role for
the crypto leg (Binance signal / Hyperliquid execution split) for exactly
the same reason: Yahoo has deep, free, keyless daily history; Hyperliquid's
own xyz candles only go back to when each market was deployed (~1yr) and
trade 24/7 including weekends, which would let a "breakout" fire on thin
weekend drift disconnected from real price discovery. Real trading-day
closes only, same discipline as the crypto side.

query1.finance.yahoo.com is unofficial/undocumented (same caveat this
project already has with Binance's main domain vs data-api.binance.vision -
see turtle_hyperliquid_lib.py). Verified working keyless from this
environment 2026-11-01; re-verify if it ever starts 403/429ing from GitHub
Actions' IP ranges specifically, the same class of failure already hit once
in this project.

Signal symbol (dict key, what state/dashboard/backtest use) is NOT always
the Hyperliquid execution symbol - they diverge for indices/commodities
(e.g. "SP500" -> Yahoo "^GSPC" -> Hyperliquid "xyz:SP500"). EQUITY_ROSTER is
the single source of truth for both mappings.
"""
import json, os, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data", "turtle_equity")
os.makedirs(DATA_DIR, exist_ok=True)

# signal symbol -> (Yahoo chart symbol, Hyperliquid xyz: execution symbol)
# Shortlist rationale (2026-11-01 research): liquidity-filtered from all 141
# xyz dex markets (only 30 clear $1M/24h volume, vs the weakest existing
# crypto token TRX at $15.6M/24h - these equities are meaningfully thinner,
# hence a short, liquidity-weighted list rather than the full board) plus a
# deliberate correlation split: broad macro/index (cleanest trends, best
# diversification away from crypto beta) + a handful of true mega-caps
# (only single names with real depth) - see conversation for the full
# liquidity table this was screened against.
EQUITY_ROSTER = {
    "XYZ100": ("^NDX",  "xyz:XYZ100"),   # Nasdaq-100 index perp
    "SP500":  ("^GSPC", "xyz:SP500"),    # S&P 500 index perp
    "GOLD":   ("GC=F",  "xyz:GOLD"),     # COMEX gold futures (continuous)
    "CL":     ("CL=F",  "xyz:CL"),       # WTI crude futures (continuous)
    "NVDA":   ("NVDA",  "xyz:NVDA"),
    "TSLA":   ("TSLA",  "xyz:TSLA"),
    "AAPL":   ("AAPL",  "xyz:AAPL"),
    "GOOGL":  ("GOOGL", "xyz:GOOGL"),
    "MSFT":   ("MSFT",  "xyz:MSFT"),
    "AMZN":   ("AMZN",  "xyz:AMZN"),
}
TOKENS = list(EQUITY_ROSTER.keys())
HL_SYMBOL = {tok: hl for tok, (_, hl) in EQUITY_ROSTER.items()}

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"


def token_file(tok):
    return os.path.join(DATA_DIR, f"{tok}.json")


def load_candles(tok):
    try:
        return json.load(open(token_file(tok)))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_candles(tok, candles):
    json.dump(candles, open(token_file(tok), "w"))


def fetch_yahoo_daily(yahoo_sym, rng="5y"):
    """Real trading-day daily OHLC only - Yahoo simply omits weekends/
    holidays from a daily-interval series (verified: ~251 bars/year, not
    365), so no filtering needed for that. Futures symbols (GC=F, CL=F) can
    carry a null bar around contract-roll/holiday gaps - dropped here."""
    url = CHART_URL.format(sym=yahoo_sym, rng=rng)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    result = d["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        day = t - (t % 86400)
        out.append({"ts": day, "open": o, "high": h, "low": l, "close": c})
    by_day = {c["ts"]: c for c in out}   # de-dupe if range windows ever overlap
    return sorted(by_day.values(), key=lambda c: c["ts"])


def update_all(now_ts=None):
    """Refresh every equity/macro token's persisted daily history. First run
    for a token (no local file) pulls 5y in one call; subsequent runs pull
    just the trailing month - cheap, matches turtle_hyperliquid_lib.py's
    update_all() shape so the live trader can treat both sources uniformly.
    Returns {tok: candles}."""
    now_ts = now_ts or int(time.time())
    out = {}
    for tok, (yahoo_sym, _) in EQUITY_ROSTER.items():
        existing = load_candles(tok)
        rng = "5y" if not existing else "1mo"
        try:
            fresh = fetch_yahoo_daily(yahoo_sym, rng=rng)
        except Exception as e:
            print(f"  {tok:<8} fetch FAILED ({e}) - keeping existing {len(existing)} bars")
            out[tok] = existing
            continue
        have = {c["ts"] for c in existing}
        merged = existing + [c for c in fresh if c["ts"] not in have]
        merged = sorted(merged, key=lambda c: c["ts"])
        save_candles(tok, merged)
        out[tok] = merged
        print(f"  {tok:<8} {len(merged):>5} daily bars (updated)")
    return out
