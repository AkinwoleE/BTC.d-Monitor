#!/usr/bin/env python3
"""
Live Telegram alerts for a small watchlist of Divergence 100 tokens (default:
PUMP, ASTER) — fully isolated from the BTC trading stack and from Divergence
100's own daily leaderboard rebuild (own data file, own workflow, own
concurrency group; reads divergence100.json only for the backtest context
line in each alert, never writes it).

Same RSI-14 / k=3-confirmed-pivot detection as everywhere else in this
project (rsi/find_divergences/update_forward reused from divergence_monitor),
2h timeframe only — matches the exact methodology Divergence 100 used to
rank these tokens, no untested 1h signal. Runs hourly (2h candles don't close
often enough to justify tighter polling); alerts once per newly confirmed
episode, same fresh-vs-backfilled distinction as the BTC monitor.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional — prints instead if unset,
via divergence_monitor.tg), ALERT_SYMBOLS (comma-separated, default below),
TP_SL_CONFIG (JSON, per-symbol suggested exit levels — see TP_SL below).
"""
import json, os, time, requests
from datetime import datetime, timezone

from divergence_monitor import rsi, find_divergences, update_forward, tg

HERE       = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(HERE, "token_alerts.json")
LB_FILE    = os.path.join(HERE, "divergence100.json")
SYMBOLS    = [s.strip().upper() for s in os.environ.get("ALERT_SYMBOLS", "PUMP,ASTER").split(",") if s.strip()]
CANDLE_LIMIT = 400          # ~33 days of 2h candles: ample for RSI-14 warmup + max 60-bar pivot spacing
FRESH_ALERT_SEC = 3 * 3600  # hourly cadence + one 2h candle period of slop

# Suggested TP/SL, in percent, from the 2026-08-28 sweep of PUMP's 61 2h
# episodes (48h forward window): the TP10/SL15 quadrant was the most robust
# region, not an isolated spike (see conversation). ASTER not swept yet.
TP_SL = json.loads(os.environ.get("TP_SL_CONFIG", '{"PUMP": {"tp": 10, "sl": 15}}'))

def fetch_2h(symbol, limit=CANDLE_LIMIT):
    # data-api.binance.vision, not api.binance.com: the latter 451s from
    # GitHub Actions' IP ranges even on public GET endpoints (geo-blocked as
    # a restricted-location trading API). This is Binance's own documented
    # market-data-only mirror, no such restriction.
    r = requests.get(f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}USDT&interval=2h&limit={limit}", timeout=15)
    r.raise_for_status()
    now = int(time.time())
    return [{"ts": row[0] // 1000, "open": float(row[1]), "high": float(row[2]),
             "low": float(row[3]), "close": float(row[4])}
            for row in r.json() if row[0] // 1000 + 7200 <= now]

def backtest_note(symbol):
    try:
        row = next(t for t in json.load(open(LB_FILE))["tokens"] if t["symbol"] == symbol)
        if row.get("avg_ret_24h") is not None:
            return (f"Divergence 100 backtest (2h, {row['history_days']}d history): "
                    f"{row['win_rate']}% win, avg {row['avg_ret_24h']:+.2f}%/24h over {row['episodes']} episodes.")
    except Exception:
        pass
    return "No Divergence 100 backtest stats available for this token yet."

def tp_sl_line(symbol, ep):
    cfg = TP_SL.get(symbol)
    if not cfg:
        return ""
    tp_pct, sl_pct = cfg["tp"], cfg["sl"]
    short = ep["direction"] == "bearish"
    ref = ep["confirm_price"]
    tp_px = ref * (1 - tp_pct / 100) if short else ref * (1 + tp_pct / 100)
    sl_px = ref * (1 + sl_pct / 100) if short else ref * (1 - sl_pct / 100)
    return f"Suggested TP/SL: +{tp_pct}% ({tp_px:.6g}) / -{sl_pct}% ({sl_px:.6g})\n"

def alert(symbol, ep):
    arrow = "🔻" if ep["direction"] == "bearish" else "🔼"
    p, legs = ep["pivots"], ep.get("legs", 3)
    seq_p = " → ".join(f"{x['price']:.6g}" for x in p)
    seq_r = " → ".join(f"{x['rsi']:.1f}" for x in p)
    age_h = (ep["confirmed_ts"] - p[-1]["ts"]) / 3600
    label = "RSI divergence" if legs == 3 else "2-pt RSI divergence"
    tg(f"{arrow} <b>{ep['direction'].upper()} {label} — {symbol} 2h</b>\n"
       f"Price: {seq_p}\nRSI:   {seq_r}\n"
       f"Confirmed @ {ep['confirm_price']:.6g} (pivot printed {age_h:.0f}h ago)\n"
       f"{tp_sl_line(symbol, ep)}"
       f"{backtest_note(symbol)}\n"
       f"Watchlist alert only — not traded automatically.")

def main():
    now = int(time.time())
    print(f"token_alerts {datetime.now(timezone.utc).isoformat()}  watching={SYMBOLS}")
    try:
        data = json.load(open(DATA_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    for symbol in SYMBOLS:
        candles = fetch_2h(symbol)
        if len(candles) < 60:
            print(f"  {symbol}: only {len(candles)} candles, skipping")
            continue
        sd = data.setdefault(symbol, {"episodes": []})
        known = {e["id"] for e in sd["episodes"]}
        rs = rsi([c["close"] for c in candles])
        new_eps = [ep for ep in find_divergences(candles, rs, "2h") if ep["id"] not in known]

        fresh_count = 0
        for ep in new_eps:
            ep["forward"] = []
            is_fresh = now - ep["confirmed_ts"] <= FRESH_ALERT_SEC
            ep["alerted"] = is_fresh
            sd["episodes"].append(ep)
            known.add(ep["id"])
            print(f"  {symbol} NEW {ep['id']} confirmed "
                  f"{datetime.fromtimestamp(ep['confirmed_ts'], tz=timezone.utc)} fresh={is_fresh}")
            if is_fresh:
                alert(symbol, ep)
                fresh_count += 1

        for ep in sd["episodes"]:
            if not ep.get("stats", {}).get("complete"):
                update_forward(ep, candles)
        sd["episodes"].sort(key=lambda e: e["confirmed_ts"])
        print(f"  {symbol}: {len(sd['episodes'])} episodes tracked ({fresh_count} new alerts)")

    data["updated"] = datetime.now(timezone.utc).isoformat()
    json.dump(data, open(DATA_FILE, "w"), indent=1)

if __name__ == "__main__":
    main()
