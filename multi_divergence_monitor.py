#!/usr/bin/env python3
"""
Multi-asset RSI divergence monitor — standalone, detection + logging only.

Fully independent of the BTC trading stack (btcd_trader.py, divergence_trader.py,
divergence_monitor.py) and their state/log files. No trading logic of any kind —
detect, log, alert only. Owns its own data file (multi_divergences.json), its own
GitHub workflow, its own cron schedule.

Reuses divergence_monitor.py's proven detection engine verbatim (RSI Wilder calc,
k=3-confirmed pivots, 3-pivot/2-leg pattern + experimental 2-pt RSI-extreme
pattern, forward-tracking stats, alert-once-per-episode dedup) — generalized from
BTC-only/hardcoded-hours to a per-asset config so the same logic runs across
different symbols, data sources, and bar durations.

Assets (see ASSETS below):
  ETH/USD  — Kraken, 1h   — same windows as the BTC monitor's 1h config
  SOL/USD  — Kraken, 1h   — same windows as the BTC monitor's 1h config
  XAU/USD  — TwelveData, 1d — forward/entry/fresh windows scaled to daily bars
             (10-day forward / 3-day entry-search / 2-day alert-freshness,
             chosen 2026-08-07 to match typical daily-chart divergence
             resolution time rather than a literal same-bar-count translation
             of the hourly windows, which would stretch to ~7 weeks)

None of these three assets have a dedicated backtest (unlike BTC's validated
3-pivot pattern) — alert text says so explicitly rather than borrowing BTC's
accuracy numbers.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (reused from the BTC monitor's
secrets), TWELVEDATA_API_KEY (gold only — https://twelvedata.com, free tier,
no credit card, ~800 req/day, XAU/USD listed as a native commodity symbol).
"""
import os, json, time, requests
from datetime import datetime, timezone

BASE       = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(BASE, "multi_divergences.json")
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
TD_KEY     = os.environ.get("TWELVEDATA_API_KEY", "")

KRAKEN     = "https://api.kraken.com/0/public"
TWELVEDATA = "https://api.twelvedata.com"

RSI_PERIOD   = 14
PIVOT_K      = 3     # confirming bars each side of a pivot — same across all assets
SPACING_MIN  = 4     # bars between consecutive pivots — pattern definition, timeframe-agnostic
SPACING_MAX  = 60
RSI_EXT_BEAR = 65     # 2-point pattern only: first pivot's RSI must be at an
RSI_EXT_BULL = 35     # extreme, else mid-range noise fires constantly

# ── per-asset config ────────────────────────────────────────────────────────────
# fwd_bars/entry_bars/fresh_alert_bars are bar COUNTS (not raw hours), so the same
# code path handles hourly and daily assets — only bar_seconds changes.
ASSETS = [
    {"symbol": "ETH/USD", "source": "kraken", "pair": "ETHUSD", "tf": "1h",
     "bar_seconds": 3600, "fwd_bars": 48, "entry_bars": 12, "fresh_alert_bars": 2,
     "horizons_bars": [6, 12, 24, 48], "unit": "h"},
    {"symbol": "SOL/USD", "source": "kraken", "pair": "SOLUSD", "tf": "1h",
     "bar_seconds": 3600, "fwd_bars": 48, "entry_bars": 12, "fresh_alert_bars": 2,
     "horizons_bars": [6, 12, 24, 48], "unit": "h"},
    {"symbol": "XAU/USD", "source": "twelvedata", "tf": "1d",
     "bar_seconds": 86400, "fwd_bars": 10, "entry_bars": 3, "fresh_alert_bars": 2,
     "horizons_bars": [2, 5, 10], "unit": "d"},
]

# ── data fetching ─────────────────────────────────────────────────────────────

def kraken_ohlc(pair, interval_min, n=700):
    """Same body as divergence_monitor.py's kraken_1h(), parameterized by pair/interval."""
    since = int(time.time()) - interval_min * 60 * (n + 5)
    r = requests.get(f"{KRAKEN}/OHLC?pair={pair}&interval={interval_min}&since={since}", timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise ValueError(f"Kraken: {d['error']}")
    key = next(k for k in d["result"] if k != "last")
    return [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4])} for c in d["result"][key]]

def twelvedata_daily(symbol, outputsize=300):
    if not TD_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY not set")
    r = requests.get(f"{TWELVEDATA}/time_series",
                     params={"symbol": symbol, "interval": "1day", "outputsize": outputsize,
                             "timezone": "UTC", "apikey": TD_KEY},
                     timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("status") == "error" or "values" not in d:
        raise ValueError(f"TwelveData: {d.get('message', d)}")
    out = []
    for v in reversed(d["values"]):   # TwelveData returns newest-first
        dt = datetime.strptime(v["datetime"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        out.append({"ts": int(dt.timestamp()), "open": float(v["open"]), "high": float(v["high"]),
                    "low": float(v["low"]), "close": float(v["close"])})
    return out

def fetch_candles(cfg):
    if cfg["source"] == "kraken":
        raw = kraken_ohlc(cfg["pair"], cfg["bar_seconds"] // 60)
    elif cfg["source"] == "twelvedata":
        raw = twelvedata_daily(cfg["symbol"])
    else:
        raise ValueError(f"unknown source {cfg['source']}")
    now = int(time.time())
    return [c for c in raw if c["ts"] + cfg["bar_seconds"] <= now]   # drop still-forming candle

# ── indicators / pattern (verbatim from divergence_monitor.py — timeframe-agnostic) ──

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

def find_divergences(candles, rsis, cfg):
    """All divergences fully confirmed within `candles`, for one asset config.

    legs=3: the BTC-backtested 3-pivot/2-leg pattern.
    legs=2: single-leg pair, extremity-filtered (first pivot RSI >= RSI_EXT_BEAR /
            <= RSI_EXT_BULL) — exploratory, no dedicated backtest for any asset here.
    """
    found = []
    iv = cfg["bar_seconds"]
    slug = cfg["symbol"].replace("/", "")
    for direction, kind in (("bearish", "high"), ("bullish", "low")):
        key = "high" if kind == "high" else "low"
        piv = [i for i in pivots(candles, kind) if rsis[i] is not None]

        def make(idx, legs):
            last = idx[-1]
            ci = last + PIVOT_K
            if ci >= len(candles):
                return None
            tag = direction if legs == 3 else f"{direction}2"
            return {
                "id": f"{slug}-{cfg['tf']}-{tag}-{candles[last]['ts']}",
                "symbol": cfg["symbol"], "tf": cfg["tf"], "direction": direction, "legs": legs,
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

def update_forward(ep, candles, cfg):
    """Extend ep['forward'] with closed candles up to cfg['fwd_bars'] past confirmation."""
    bs  = cfg["bar_seconds"]
    end = ep["confirmed_ts"] + cfg["fwd_bars"] * bs
    have = {c["ts"] for c in ep.get("forward", [])}
    now  = int(time.time())
    for c in candles:
        if ep["confirmed_ts"] <= c["ts"] < end and c["ts"] + bs <= now and c["ts"] not in have:
            ep.setdefault("forward", []).append(
                {"ts": c["ts"], "high": c["high"], "low": c["low"], "close": c["close"]})
    ep.setdefault("forward", []).sort(key=lambda c: c["ts"])
    compute_stats(ep, cfg)

def compute_stats(ep, cfg):
    fwd = ep.get("forward", [])
    if not fwd:
        return
    bs, unit = cfg["bar_seconds"], cfg["unit"]
    short = ep["direction"] == "bearish"
    ref   = ep["confirm_price"]

    def ret_at(n_bars):
        cutoff = ep["confirmed_ts"] + n_bars * bs
        cands  = [c for c in fwd if c["ts"] + bs <= cutoff]
        if not cands:
            return None
        px = cands[-1]["close"]
        return round(100 * ((ref - px) / ref if short else (px - ref) / ref), 3)

    entry_end = ep["confirmed_ts"] + cfg["entry_bars"] * bs
    ewin = [c for c in fwd if c["ts"] < entry_end]
    stats = {"complete": len(fwd) >= cfg["fwd_bars"]}
    for n in cfg["horizons_bars"]:
        stats[f"ret_{n}{unit}"] = ret_at(n)
    if ewin:
        be = max(ewin, key=lambda c: c["high"]) if short else min(ewin, key=lambda c: c["low"])
        e_px = be["high"] if short else be["low"]
        stats["best_entry"] = {"ts": be["ts"], "price": e_px,
                               f"{unit}_after": round((be["ts"] - ep["confirmed_ts"]) / bs, 1)}
        xwin = [c for c in fwd if c["ts"] >= be["ts"]]
        bx = min(xwin, key=lambda c: c["low"]) if short else max(xwin, key=lambda c: c["high"])
        x_px = bx["low"] if short else bx["high"]
        stats["best_exit"] = {"ts": bx["ts"], "price": x_px,
                              f"{unit}_after": round((bx["ts"] - ep["confirmed_ts"]) / bs, 1)}
        stats["best_pnl_pct"] = round(100 * ((e_px - x_px) / e_px if short else (x_px - e_px) / e_px), 3)
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

def fmt_px(v):
    return f"{v:,.2f}" if v < 1000 else f"{v:,.0f}"

def alert(ep, cfg):
    arrow = "🔻" if ep["direction"] == "bearish" else "🔼"
    p = ep["pivots"]
    legs = ep.get("legs", 3)
    seq_p = " → ".join(fmt_px(x["price"]) for x in p)
    seq_r = " → ".join(f"{x['rsi']:.1f}" for x in p)
    age = (ep["confirmed_ts"] - p[-1]["ts"]) / cfg["bar_seconds"]
    if legs == 2:
        hist = ("2-pt pattern (RSI-extreme filtered) — same detection logic as the BTC "
                "monitor, no dedicated backtest for this asset. Exploratory.")
    else:
        hist = ("3-pivot pattern — same rule validated on BTC (86% bearish / 50% bullish "
                "accuracy there), not separately backtested for this asset/timeframe.")
    label = "RSI divergence" if legs == 3 else "2-pt RSI divergence"
    tg(f"{arrow} <b>{ep['direction'].upper()} {label} — {ep['symbol']} {ep['tf']}</b>\n"
       f"Price: {seq_p}\n"
       f"RSI:   {seq_r}\n"
       f"Confirmed @ {fmt_px(ep['confirm_price'])} (pivot printed {age:.0f}{cfg['unit']} ago)\n"
       f"{hist}\n"
       f"Detection/logging only — no trade taken by this monitor.")

def already_alerted_similar(ep, data, cfg):
    """Scoped to the SAME symbol only — an ETH divergence must never suppress a SOL one."""
    sym = ep["symbol"]
    tol = 2 * cfg["bar_seconds"]
    t = ep["pivots"][-1]["ts"]
    return any(o.get("alerted") and o.get("symbol") == sym and o["direction"] == ep["direction"]
               and abs(o["pivots"][-1]["ts"] - t) <= tol
               for o in data["episodes"])

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    now = int(time.time())
    print(f"multi_divergence_monitor {datetime.now(timezone.utc).isoformat()}")

    try:
        data = json.load(open(DATA_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"episodes": []}
    known = {e["id"]: e for e in data["episodes"]}

    total_alerts = 0
    for cfg in ASSETS:
        print(f"  --- {cfg['symbol']} ({cfg['tf']}, {cfg['source']}) ---")
        try:
            candles = fetch_candles(cfg)
        except Exception as e:
            print(f"    fetch failed: {e} — skipping this asset for this run")
            continue
        print(f"    candles: {len(candles)}")
        if len(candles) < RSI_PERIOD + 2 * PIVOT_K + 3:
            print("    not enough candles yet — skipping")
            continue

        rs = rsi([c["close"] for c in candles])
        new_eps = [ep for ep in find_divergences(candles, rs, cfg) if ep["id"] not in known]
        new_eps.sort(key=lambda e: -e.get("legs", 3))   # 3-pivot alerts before its 2-pt sub-pair

        fresh_window = cfg["fresh_alert_bars"] * cfg["bar_seconds"]
        for ep in new_eps:
            ep["forward"] = []
            is_fresh = now - ep["confirmed_ts"] <= fresh_window
            dup = is_fresh and already_alerted_similar(ep, data, cfg)
            ep["alerted"] = bool(is_fresh and not dup)
            if dup:
                ep["alert_suppressed"] = "duplicate-structure"
            data["episodes"].append(ep)
            known[ep["id"]] = ep
            print(f"    NEW {ep['id']} confirmed {datetime.fromtimestamp(ep['confirmed_ts'], tz=timezone.utc)} "
                  f"fresh={is_fresh}{' (alert suppressed: duplicate structure)' if dup else ''}")
            if ep["alerted"]:
                alert(ep, cfg)
                total_alerts += 1

        for ep in data["episodes"]:
            if ep["symbol"] == cfg["symbol"] and not ep.get("stats", {}).get("complete"):
                update_forward(ep, candles, cfg)

    data["episodes"].sort(key=lambda e: e["confirmed_ts"])
    data["updated"] = datetime.now(timezone.utc).isoformat()
    json.dump(data, open(DATA_FILE, "w"), indent=1)
    print(f"  saved {len(data['episodes'])} total episodes across {len(ASSETS)} assets ({total_alerts} new alerts)")

if __name__ == "__main__":
    main()
