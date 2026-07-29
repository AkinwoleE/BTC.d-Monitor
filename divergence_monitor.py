#!/usr/bin/env python3
"""
BTC RSI divergence monitor — standalone, detection + logging only (no trading).

Detects 2-leg (3-pivot) RSI-14 divergences on BTC/USD, on 1h and synthetic 2h
candles (Kraken has no native 2h interval; pairs of UTC-even-hour 1h candles
are resampled to match the Binance backtest's boundary convention).

Pattern (per the 2026-07 backtest):
  pivot        = local high/low with k=3 confirming bars each side
                 (so a pivot is only knowable ~3 candles after it prints)
  bearish      = 3 consecutive pivot HIGHS, price strictly rising (HH,HH)
                 while RSI strictly falls (LH,LH)
  bullish      = mirror on pivot LOWS
  spacing      = 4..60 bars between consecutive pivots

On a newly confirmed episode: Telegram alert (only if confirmation is recent —
backfilled/stale episodes are recorded silently) and append to divergences.json.
Every run also extends each young episode's forward candles (up to 48h past
confirmation) and recomputes its stats (returns at horizons, MFE/MAE, best
entry within the first 12h, best exit after that entry). The dashboard
(divergence.html) is purely presentational on top of this file.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional — prints instead if unset).
"""
import os, json, time, requests
from datetime import datetime, timezone

KRAKEN     = "https://api.kraken.com/0/public"
PAIR       = "XBTUSD"
DATA_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "divergences.json")
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")

RSI_PERIOD   = 14
PIVOT_K      = 3          # confirming bars each side of a pivot
SPACING_MIN  = 4          # bars between consecutive pivots
SPACING_MAX  = 60
FWD_HOURS    = 48         # forward tracking window after confirmation
ENTRY_HOURS  = 12         # "best entry" search window after confirmation
FRESH_ALERT  = {"1h": 2 * 3600, "2h": 4 * 3600}   # alert only if confirmed this recently

# ── data ──────────────────────────────────────────────────────────────────────

def kraken_1h(n=700):
    since = int(time.time()) - 3600 * (n + 5)
    r = requests.get(f"{KRAKEN}/OHLC?pair={PAIR}&interval=60&since={since}", timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise ValueError(f"Kraken: {d['error']}")
    key = next(k for k in d["result"] if k != "last")
    return [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4])} for c in d["result"][key]]

def resample_2h(c1h):
    """Pair UTC-even-hour-anchored 1h candles into synthetic 2h candles."""
    out = []
    i = 0
    while i < len(c1h) - 1:
        a = c1h[i]
        if (a["ts"] // 3600) % 2 != 0:
            i += 1
            continue
        b = c1h[i + 1]
        if b["ts"] != a["ts"] + 3600:   # gap — skip the incomplete pair
            i += 1
            continue
        out.append({"ts": a["ts"], "open": a["open"], "close": b["close"],
                    "high": max(a["high"], b["high"]), "low": min(a["low"], b["low"])})
        i += 2
    return out

# ── indicators / pattern ──────────────────────────────────────────────────────

def rsi(closes, period=RSI_PERIOD):
    """Wilder-smoothed RSI; None until warm."""
    out   = [None] * len(closes)
    ag = al = None
    g = l = 0.0
    for i in range(1, len(closes)):
        ch   = closes[i] - closes[i - 1]
        gain = max(ch, 0.0)
        loss = max(-ch, 0.0)
        if i <= period:
            g += gain; l += loss
            if i == period:
                ag, al = g / period, l / period
                out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            ag = (ag * (period - 1) + gain) / period
            al = (al * (period - 1) + loss) / period
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

def pivots(candles, kind):
    """Indices of confirmed pivot highs/lows (k bars strictly lower/higher each side)."""
    key = "high" if kind == "high" else "low"
    idx = []
    for i in range(PIVOT_K, len(candles) - PIVOT_K):
        v = candles[i][key]
        if kind == "high":
            if all(candles[j][key] < v for j in range(i - PIVOT_K, i + PIVOT_K + 1) if j != i):
                idx.append(i)
        else:
            if all(candles[j][key] > v for j in range(i - PIVOT_K, i + PIVOT_K + 1) if j != i):
                idx.append(i)
    return idx

def find_divergences(candles, rsis, tf):
    """All 3-pivot divergences fully confirmed within `candles`."""
    found = []
    for direction, kind in (("bearish", "high"), ("bullish", "low")):
        key = "high" if kind == "high" else "low"
        piv = [i for i in pivots(candles, kind) if rsis[i] is not None]
        for a in range(len(piv) - 2):
            i1, i2, i3 = piv[a], piv[a + 1], piv[a + 2]
            if not (SPACING_MIN <= i2 - i1 <= SPACING_MAX and
                    SPACING_MIN <= i3 - i2 <= SPACING_MAX):
                continue
            p1, p2, p3 = candles[i1][key], candles[i2][key], candles[i3][key]
            r1, r2, r3 = rsis[i1], rsis[i2], rsis[i3]
            if direction == "bearish":
                ok = p1 < p2 < p3 and r1 > r2 > r3
            else:
                ok = p1 > p2 > p3 and r1 < r2 < r3
            if not ok:
                continue
            ci = i3 + PIVOT_K                      # bar whose close confirms pivot 3
            if ci >= len(candles):
                continue
            iv = 3600 if tf == "1h" else 7200
            found.append({
                "id": f"{tf}-{direction}-{candles[i3]['ts']}",
                "tf": tf, "direction": direction,
                "pivots": [{"ts": candles[i]["ts"], "price": candles[i][key],
                            "rsi": round(rsis[i], 2)} for i in (i1, i2, i3)],
                "confirmed_ts": candles[ci]["ts"] + iv,   # close time of confirming bar
                "confirm_price": candles[ci]["close"],
            })
    return found

# ── forward tracking / stats ──────────────────────────────────────────────────

def update_forward(ep, c1h):
    """Extend ep['forward'] with closed 1h candles up to FWD_HOURS past confirmation."""
    end  = ep["confirmed_ts"] + FWD_HOURS * 3600
    have = {c["ts"] for c in ep.get("forward", [])}
    now  = int(time.time())
    for c in c1h:
        # candle must open at/after confirmation, be closed, and inside the window
        if ep["confirmed_ts"] <= c["ts"] < end and c["ts"] + 3600 <= now and c["ts"] not in have:
            ep.setdefault("forward", []).append(
                {"ts": c["ts"], "high": c["high"], "low": c["low"], "close": c["close"]})
    ep.setdefault("forward", []).sort(key=lambda c: c["ts"])
    compute_stats(ep)

def compute_stats(ep):
    fwd = ep.get("forward", [])
    if not fwd:
        return
    short = ep["direction"] == "bearish"
    ref   = ep["confirm_price"]
    def ret_at(h):
        cutoff = ep["confirmed_ts"] + h * 3600
        cands  = [c for c in fwd if c["ts"] + 3600 <= cutoff]
        if not cands:
            return None
        px = cands[-1]["close"]
        return round(100 * ((ref - px) / ref if short else (px - ref) / ref), 3)

    # best entry: most favorable price in the first ENTRY_HOURS
    ewin = [c for c in fwd if c["ts"] < ep["confirmed_ts"] + ENTRY_HOURS * 3600]
    stats = {"complete": len(fwd) >= FWD_HOURS,
             "ret_6h": ret_at(6), "ret_12h": ret_at(12),
             "ret_24h": ret_at(24), "ret_48h": ret_at(48)}
    if ewin:
        be = max(ewin, key=lambda c: c["high"]) if short else min(ewin, key=lambda c: c["low"])
        e_px = be["high"] if short else be["low"]
        stats["best_entry"] = {"ts": be["ts"], "price": e_px,
                               "hours_after": round((be["ts"] - ep["confirmed_ts"]) / 3600, 1)}
        # best exit: most favorable price at/after the best-entry bar
        xwin = [c for c in fwd if c["ts"] >= be["ts"]]
        bx = min(xwin, key=lambda c: c["low"]) if short else max(xwin, key=lambda c: c["high"])
        x_px = bx["low"] if short else bx["high"]
        stats["best_exit"] = {"ts": bx["ts"], "price": x_px,
                              "hours_after": round((bx["ts"] - ep["confirmed_ts"]) / 3600, 1)}
        stats["best_pnl_pct"] = round(100 * ((e_px - x_px) / e_px if short else (x_px - e_px) / e_px), 3)
        # MFE / MAE from confirmation price over the whole window
        hi = max(c["high"] for c in fwd); lo = min(c["low"] for c in fwd)
        stats["mfe_pct"] = round(100 * ((ref - lo) / ref if short else (hi - ref) / ref), 3)
        stats["mae_pct"] = round(100 * ((hi - ref) / ref if short else (ref - lo) / ref), 3)
    ep["stats"] = stats

# ── alerting ──────────────────────────────────────────────────────────────────

def tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"  [no telegram creds] would send:\n{msg}")
        return
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    print("  Telegram OK" if r.ok and r.json().get("ok") else f"  Telegram FAIL: {r.text[:100]}")

def alert(ep):
    arrow = "🔻" if ep["direction"] == "bearish" else "🔼"
    p = ep["pivots"]
    seq_p = " → ".join(f"{x['price']:,.0f}" for x in p)
    seq_r = " → ".join(f"{x['rsi']:.1f}" for x in p)
    age_h = (ep["confirmed_ts"] - p[2]["ts"]) / 3600
    if ep["direction"] == "bearish":
        hist = "History (1h backtest): 86% right-direction, mean ≈ −1% @6–24h. 2h: rarer, similar edge."
    else:
        hist = "⚠️ History: bullish divergences were a coin flip (50%) in backtest — weak signal, size accordingly."
    tg(f"{arrow} <b>{ep['direction'].upper()} RSI divergence — BTC {ep['tf']}</b>\n"
       f"Price: {seq_p}\n"
       f"RSI:   {seq_r}\n"
       f"Confirmed @ {ep['confirm_price']:,.0f} (pivot printed {age_h:.0f}h ago)\n"
       f"{hist}\n"
       f"Manual trade — bot will track fwd 48h on the dashboard.")

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    now = int(time.time())
    print(f"divergence_monitor {datetime.now(timezone.utc).isoformat()}")

    c1h_all = kraken_1h()
    # drop the still-forming last candle
    c1h = [c for c in c1h_all if c["ts"] + 3600 <= now]
    c2h = [c for c in resample_2h(c1h) if c["ts"] + 7200 <= now]
    print(f"  candles: {len(c1h)}x1h  {len(c2h)}x2h")

    try:
        data = json.load(open(DATA_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"episodes": []}
    known = {e["id"]: e for e in data["episodes"]}

    fresh_alerts = 0
    for candles, tf in ((c1h, "1h"), (c2h, "2h")):
        rs = rsi([c["close"] for c in candles])
        for ep in find_divergences(candles, rs, tf):
            if ep["id"] in known:
                continue
            is_fresh = now - ep["confirmed_ts"] <= FRESH_ALERT[tf]
            ep["alerted"] = bool(is_fresh)
            ep["forward"] = []
            data["episodes"].append(ep)
            known[ep["id"]] = ep
            print(f"  NEW {ep['id']} confirmed {datetime.fromtimestamp(ep['confirmed_ts'], tz=timezone.utc)} fresh={is_fresh}")
            if is_fresh:
                alert(ep)
                fresh_alerts += 1

    # forward-track anything not yet complete
    for ep in data["episodes"]:
        if not ep.get("stats", {}).get("complete"):
            update_forward(ep, c1h)

    data["episodes"].sort(key=lambda e: e["confirmed_ts"])
    data["updated"] = datetime.now(timezone.utc).isoformat()
    json.dump(data, open(DATA_FILE, "w"), indent=1)
    print(f"  saved {len(data['episodes'])} episodes ({fresh_alerts} new alerts)")

if __name__ == "__main__":
    main()
