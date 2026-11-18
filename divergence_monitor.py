#!/usr/bin/env python3
"""
BTC RSI divergence monitor — standalone, detection + logging only (no trading).

Detects 2-leg (3-pivot) and extremity-filtered 2-pt RSI-14 divergences on BTC.
1h candles still come from Kraken (dashboard only — the trader does not act on
them). 2h candles, which the trader does act on, come from Binance BTCUSDT
native 2h klines (data-api.binance.vision, the same source as divergence100),
merged with the committed data/divergence100/BTCUSDT.json archive so RSI is
computed on the long history the backtest uses. If that Binance series does
not reach the latest closed bar, detection falls back to Kraken 1h candles
resampled into UTC-even 2h bars and says so in the log (and, at most once per
6h, on Telegram).

Pattern (per the 2026-07 backtest):
  pivot        = local high/low with k=3 confirming bars each side
                 (so a pivot is only knowable ~3 candles after it prints)
  bearish      = 3 consecutive pivot HIGHS, price strictly rising (HH,HH)
                 while RSI strictly falls (LH,LH)
  bullish      = mirror on pivot LOWS
  spacing      = 4..60 bars between consecutive pivots

Pivots inside the RSI warm-up (the first RSI_WARMUP_BARS of the series) are
ignored. Wilder's seed still moves RSI by several points that far from the
left edge of a short window, which is how a ~700-candle Kraken fetch used to
append "new" episodes weeks after they had confirmed, with a different RSI
than the same pivot got in another record. Episodes that are already older
than FRESH_ALERT at detection time are not recorded at all.

On a newly confirmed fresh episode: Telegram alert and append to
divergences.json. Every run also extends each young episode's forward candles
(up to 48h past confirmation) and recomputes its stats. 2h episodes whose
pivots sit in the reliable part of the Binance series but which that series
does not produce are tagged phantom so the dashboard can keep them visible
without counting them in the headline stats.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional — prints instead if unset).

See docs/DIVERGENCE.md for the full system, the 2026-11-18 changelog, and
which repository variables the owner must clear.
"""
import os, json, time, requests
from datetime import datetime, timezone

KRAKEN     = "https://api.kraken.com/0/public"
PAIR       = "XBTUSD"
# data-api.binance.vision, not api.binance.com: the public market-data host
# divergence100 already uses. api.binance.com returns 451 from GitHub Actions.
BINANCE_2H_URL = ("https://data-api.binance.vision/api/v3/klines"
                  "?symbol=BTCUSDT&interval=2h&limit={limit}")
ARCHIVE_2H = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "divergence100", "BTCUSDT.json")
DATA_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "divergences.json")
BAR_1H     = 3600
BAR_2H     = 7200
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")

RSI_PERIOD   = 14
PIVOT_K      = 3          # confirming bars each side of a pivot
SPACING_MIN  = 4          # bars between consecutive pivots
SPACING_MAX  = 60
RSI_EXT_BEAR = 65         # 2-point pattern only: first pivot's RSI must be at an
RSI_EXT_BULL = 35         # extreme, else mid-range noise fires ~2x/week per side
# 2026-11-18: ignore pivots inside the Wilder warm-up. A ~700-candle Kraken
# window re-seeded RSI at its left edge, so the same pivot was recorded later
# with a different RSI and ~25-day-old "new" episodes landed in divergences.json
# (24 of 40 2h signals since Aug). Seed influence is ~0.03 RSI at bar 100 and
# ~0 by bar 150. Live detection passes this; the default find_divergences()
# call does not, so the full-history Binance backtest is unchanged.
RSI_WARMUP_BARS = 150
FWD_HOURS    = 48         # forward tracking window after confirmation
ENTRY_HOURS  = 12         # "best entry" search window after confirmation
FRESH_ALERT  = {"1h": 2 * 3600, "2h": 4 * 3600}   # record + alert only this recently
FALLBACK_ALERT_EVERY = 6 * 3600

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

def load_btcusdt_archive(path=None):
    """Committed Binance 2h history written by the divergence100 pipeline."""
    path = path or ARCHIVE_2H
    rows = json.load(open(path))["candles"]
    return [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows]

def fetch_binance_2h(limit=1000):
    """Latest native BTCUSDT 2h klines. Raises if the host is unreachable or empty."""
    r = requests.get(BINANCE_2H_URL.format(limit=int(limit)), timeout=15)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"Binance 2h: empty response {str(rows)[:120]}")
    out = []
    for row in rows:
        out.append({"ts": int(row[0]) // 1000, "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4])})
    return out

def merge_candles(*groups):
    """Concatenate candle lists; later groups overwrite the same open time."""
    by = {}
    for group in groups:
        for c in group or []:
            by[c["ts"]] = c
    return [by[t] for t in sorted(by)]

def closed_only(candles, now, bar_sec):
    return [c for c in candles if c["ts"] + bar_sec <= now]

def expected_closed_open_ts(now, bar_sec):
    """Open time of the most recent bar that has fully closed."""
    return (int(now) // bar_sec) * bar_sec - bar_sec

def assemble_2h(now, archive, live, kraken_1h):
    """Choose the 2h series detection trades on.

    2026-11-18: Binance is the trading feed. Three live trades (net about
    −$21) came from Kraken signals this series does not produce. Kraken
    resampled 2h is used only when Binance does not reach the latest closed bar.

    Returns (candles, source, note). `source` is "binance" when the merged
    Binance series reaches the latest closed bar (live rows win over the
    archive on overlap), otherwise "kraken-fallback". `note` is None on a
    clean live fetch, or a short reason when we used the archive or Kraken.
    """
    merged = closed_only(merge_candles(archive, live), now, BAR_2H)
    expect = expected_closed_open_ts(now, BAR_2H)
    if merged and merged[-1]["ts"] >= expect:
        if live:
            return merged, "binance", None
        return merged, "binance", ("live Binance fetch failed; using the divergence100 "
                                    "BTCUSDT archive, which still reaches the latest closed bar")
    parts = []
    if not archive and not live:
        parts.append("no Binance candles")
    elif not merged:
        parts.append("Binance returned candles but none were closed")
    else:
        parts.append(f"Binance series ends at open {merged[-1]['ts']}, latest closed bar opens at {expect}")
    synth = closed_only(resample_2h(kraken_1h or []), now, BAR_2H)
    return synth, "kraken-fallback", "; ".join(parts)

def episode_is_fresh(ep, now):
    """True when the signal bar has just closed — old enough to record, not a backfill.

    2026-11-18: episodes older than FRESH_ALERT used to be appended silently.
    That is what filled divergences.json with warm-up artifacts weeks late.
    The trader already ignored them; the dashboard stats did not.
    """
    limit = FRESH_ALERT.get(ep.get("tf"), 4 * 3600)
    return now - ep["confirmed_ts"] <= limit

def select_new_episodes(found, known_ids, now, source, tf):
    """Episodes to append. Already-known ids and signals that are old right now are dropped."""
    out = []
    for ep in found:
        if ep["id"] in known_ids:
            continue
        if not episode_is_fresh(ep, now):
            age_h = (now - ep["confirmed_ts"]) / 3600
            print(f"  skip stale {ep['id']} age={age_h:.1f}h (not recording warm-up/backfill)")
            continue
        ep["forward"] = []
        ep["source"] = source if tf == "2h" else "kraken"
        out.append(ep)
    return out

def mark_phantoms(episodes, found, candles, min_pivot_index):
    """Tag 2h episodes the current Binance series does not produce.

    2026-11-18: Kraken-only episodes stay in the file (the trades were real)
    but the dashboard drops them from the headline stats. Only a first pivot
    past this series' warm-up is judged, so a short fetch cannot condemn
    history it cannot see. Returns how many episode records changed.
    """
    if candles is None or len(candles) <= min_pivot_index:
        return 0
    earliest = candles[min_pivot_index]["ts"]
    ids = {e["id"] for e in found}
    changed = 0
    for ep in episodes:
        if ep.get("tf") != "2h":
            continue
        pivs = ep.get("pivots") or []
        if not pivs or pivs[0]["ts"] < earliest:
            continue
        phantom = ep["id"] not in ids
        if phantom and not ep.get("phantom"):
            ep["phantom"] = True
            changed += 1
        elif not phantom and ep.get("phantom"):
            ep.pop("phantom", None)
            changed += 1
    return changed

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

def find_divergences(candles, rsis, tf, min_pivot_index=0):
    """All divergences fully confirmed within `candles`.

    legs=3: the backtested 3-pivot/2-leg pattern.
    legs=2: single-leg pair, extremity-filtered (first pivot RSI >= RSI_EXT_BEAR /
            <= RSI_EXT_BULL) — unvalidated by the backtest, tracked to build a live
            sample. A 3-pivot episode's sub-pairs may also appear as 2-pt episodes.

    min_pivot_index drops pivots in the RSI warm-up at the left edge. The default
    is 0, which matches the historical behavior (RSI is already None until bar
    RSI_PERIOD), so a full-history backtest does not change. Live detection passes
    RSI_WARMUP_BARS.
    """
    found = []
    iv = 3600 if tf == "1h" else 7200
    for direction, kind in (("bearish", "high"), ("bullish", "low")):
        key = "high" if kind == "high" else "low"
        piv = [i for i in pivots(candles, kind)
               if i >= min_pivot_index and rsis[i] is not None]

        def make(idx, legs):
            last = idx[-1]
            ci = last + PIVOT_K                    # bar whose close confirms the final pivot
            if ci >= len(candles):
                return None
            tag = direction if legs == 3 else f"{direction}2"
            return {
                "id": f"{tf}-{tag}-{candles[last]['ts']}",
                "tf": tf, "direction": direction, "legs": legs,
                "pivots": [{"ts": candles[i]["ts"], "price": candles[i][key],
                            "rsi": round(rsis[i], 2)} for i in idx],
                "confirmed_ts": candles[ci]["ts"] + iv,
                "confirm_price": candles[ci]["close"],
            }

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
            if ok and (ep := make((i1, i2, i3), 3)):
                found.append(ep)

        for a in range(len(piv) - 1):
            i1, i2 = piv[a], piv[a + 1]
            if not SPACING_MIN <= i2 - i1 <= SPACING_MAX:
                continue
            p1, p2 = candles[i1][key], candles[i2][key]
            r1, r2 = rsis[i1], rsis[i2]
            if direction == "bearish":
                ok = p1 < p2 and r1 > r2 and r1 >= RSI_EXT_BEAR
            else:
                ok = p1 > p2 and r1 < r2 and r1 <= RSI_EXT_BULL
            if ok and (ep := make((i1, i2), 2)):
                found.append(ep)
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
    legs = ep.get("legs", 3)
    seq_p = " → ".join(f"{x['price']:,.0f}" for x in p)
    seq_r = " → ".join(f"{x['rsi']:.1f}" for x in p)
    age_h = (ep["confirmed_ts"] - p[-1]["ts"]) / 3600
    if legs == 2:
        hist = ("2-pt pattern (RSI-extreme filtered) — NOT in the backtest, "
                "~2/week expected; treat as lower confidence while live data accumulates.")
    elif ep["direction"] == "bearish":
        hist = "History (1h backtest): 86% right-direction, mean ≈ −1% @6–24h. 2h: rarer, similar edge."
    else:
        hist = "⚠️ History: bullish divergences were a coin flip (50%) in backtest — weak signal, size accordingly."
    label = "RSI divergence" if legs == 3 else "2-pt RSI divergence"
    tg(f"{arrow} <b>{ep['direction'].upper()} {label} — BTC {ep['tf']}</b>\n"
       f"Price: {seq_p}\n"
       f"RSI:   {seq_r}\n"
       f"Confirmed @ {ep['confirm_price']:,.0f} (pivot printed {age_h:.0f}h ago)\n"
       f"{hist}\n"
       f"Manual trade — bot will track fwd 48h on the dashboard.")

# ── main ──────────────────────────────────────────────────────────────────────

def load_candles(now):
    """(c1h, c2h, source, note). Kraken failure does not block a good Binance 2h series."""
    try:
        c1h = closed_only(kraken_1h(), now, BAR_1H)
        kraken_err = None
    except Exception as e:
        c1h, kraken_err = [], e
        print(f"  WARNING: Kraken 1h fetch failed ({e}). "
              "1h detection and forward tracking skipped this run.")
    archive, live = [], []
    try:
        archive = load_btcusdt_archive()
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError) as e:
        print(f"  Binance archive unavailable ({e})")
    try:
        live = fetch_binance_2h()
    except Exception as e:
        print(f"  WARNING: Binance 2h live fetch failed ({e})")
    c2h, source, note = assemble_2h(now, archive, live, c1h)
    if kraken_err and not c1h and source == "kraken-fallback":
        note = f"{note}; Kraken 1h also failed ({kraken_err})" if note else str(kraken_err)
    return c1h, c2h, source, note

def main():
    now = int(time.time())
    print(f"divergence_monitor {datetime.now(timezone.utc).isoformat()}")

    c1h, c2h, source, note = load_candles(now)
    extra = f" — {note}" if note else ""
    print(f"  candles: {len(c1h)}x1h  {len(c2h)}x2h  2h-source={source}{extra}")
    if not c2h:
        raise RuntimeError(f"no 2h candles for detection ({note or 'unknown'})")

    try:
        data = json.load(open(DATA_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"episodes": []}
    if "episodes" not in data:
        data["episodes"] = []
    known = {e["id"]: e for e in data["episodes"]}

    # 2026-11-18: 2h detection for trading uses Binance. Kraken resampled 2h is
    # only the fallback, and it is logged (and Telegram'd at most every 6h)
    # because that feed produced signals the Binance backtest never had.
    if source == "kraken-fallback":
        print("  WARNING: Binance 2h unreachable "
              f"({note}). Falling back to Kraken 1h resampled into 2h. "
              "Signals can differ from the Binance backtest until Binance is reachable again.")
        last = data.get("binance_fallback_alert_ts") or 0
        if now - last >= FALLBACK_ALERT_EVERY:
            tg("⚠️ Divergence monitor: Binance 2h candles unreachable "
               f"({note}). Detection fell back to Kraken resampled 2h. "
               "Live signals may not match the Binance backtest.")
            data["binance_fallback_alert_ts"] = now

    found_2h = []
    new_eps = []
    for candles, tf in ((c1h, "1h"), (c2h, "2h")):
        if not candles:
            print(f"  skip {tf}: no candles")
            continue
        rs = rsi([c["close"] for c in candles])
        found = find_divergences(candles, rs, tf, min_pivot_index=RSI_WARMUP_BARS)
        if tf == "2h":
            found_2h = found
        new_eps.extend(select_new_episodes(found, known, now, source, tf))

    if source == "binance" and c2h:
        n = mark_phantoms(data["episodes"], found_2h, c2h, RSI_WARMUP_BARS)
        if n:
            print(f"  phantom tags updated on {n} 2h episodes")

    # One alert per divergence structure: a 3-pivot's 2-pt sub-pair, or the same
    # setup on 1h and 2h, must not each ping. Suppress the alert (episode is
    # still recorded) when an already-alerted episode shares the direction and
    # a final pivot within 2h. Process 3-pivot before 2-pt, 1h before 2h, so
    # the stronger/finer pattern is the one that alerts.
    def already_alerted_similar(ep):
        t = ep["pivots"][-1]["ts"]
        return any(o.get("alerted") and o["direction"] == ep["direction"]
                   and abs(o["pivots"][-1]["ts"] - t) <= 2 * 3600
                   for o in data["episodes"])

    new_eps.sort(key=lambda e: (-e.get("legs", 3), e["tf"]))
    fresh_alerts = 0
    for ep in new_eps:
        # Everything in new_eps already passed episode_is_fresh.
        dup = already_alerted_similar(ep)
        ep["alerted"] = not dup
        if dup:
            ep["alert_suppressed"] = "duplicate-structure"
        data["episodes"].append(ep)
        known[ep["id"]] = ep
        print(f"  NEW {ep['id']} confirmed {datetime.fromtimestamp(ep['confirmed_ts'], tz=timezone.utc)} "
              f"fresh=True{' (alert suppressed: duplicate structure)' if dup else ''}")
        if ep["alerted"]:
            alert(ep)
            fresh_alerts += 1

    # forward-track anything not yet complete (needs 1h candles; skipped if Kraken is down)
    if c1h:
        for ep in data["episodes"]:
            if not ep.get("stats", {}).get("complete"):
                update_forward(ep, c1h)

    data["episodes"].sort(key=lambda e: e["confirmed_ts"])
    data["updated"] = datetime.now(timezone.utc).isoformat()
    json.dump(data, open(DATA_FILE, "w"), indent=1)
    print(f"  saved {len(data['episodes'])} episodes ({fresh_alerts} new alerts)")

if __name__ == "__main__":
    main()
