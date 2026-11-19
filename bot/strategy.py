#!/usr/bin/env python3
"""Hourly funding-hedge strategy.

Reads live positions (source of truth), logs an hourly funding snapshot, and
decides entries, exits, and naked-leg repairs. Orders go out only when
LIVE_TRADING is true. BOT_ENABLED=false or bot/TRADING_PAUSED blocks new
entries; risk exits and repairs still run in live mode.

The Actions log gets one summary line. Balances and addresses are not printed.
"""
import argparse
import csv
import datetime
import json
import math
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alerts import comment_if_needed
from redact import sanitize

HERE = os.path.dirname(os.path.abspath(__file__))
DEC_API = "https://api.mainnet.aptoslabs.com/decibel/api/v1"
APTOS_NODE = "https://fullnode.mainnet.aptoslabs.com/v1"
HL_INFO = "https://api.hyperliquid.xyz/info"
DEC_PKG = "0x50ead22afd6ffd9769e3b3d6e0e64a2a350d68e8b102c4e72e33d0b8cfdfdb06"
BTC_MARKET = "0x5e0e16f34adfb4b316f8d532d68acbfa206826feaaa418d3938046bdc2044861"
BTC_SPEC = {
    "coin": "BTC",
    "dec_name": "BTC/USD",
    "dec_addr": BTC_MARKET,
    "sz_decimals": 8,
    "px_decimals": 6,
    "tick": 0.1,
    "lot": 1000 / 1e8,
    "min_size": 2000 / 1e8,
    "hl_sz_decimals": 5,
    "certified_btc": True,
}
CSV_FIELDS = [
    "hour", "coin", "dec_funding_hr", "hl_funding_hr",
    "dec_oi_base", "hl_oi_base", "dec_mid", "hl_mid", "dec_mark", "hl_mark",
]


def load_config(path=None):
    with open(path or os.path.join(HERE, "config.json")) as f:
        return json.load(f)


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def paused(cfg_dir=HERE):
    flag = os.environ.get("BOT_ENABLED", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return True
    return os.path.exists(os.path.join(cfg_dir, "TRADING_PAUSED"))


def paper_mode():
    return not truthy(os.environ.get("LIVE_TRADING"))


def hourly_net(direction, dec_hr, hl_hr):
    """Positive means the hedge earns. Positive venue funding: longs pay shorts."""
    if direction == "short-dec-long-hl":
        return dec_hr - hl_hr
    if direction == "long-dec-short-hl":
        return hl_hr - dec_hr
    raise ValueError(direction)


def choose_direction(dec_hr, hl_hr):
    short_dec = dec_hr - hl_hr
    long_dec = -short_dec
    if short_dec > 0 and short_dec >= long_dec:
        return "short-dec-long-hl", short_dec
    if long_dec > 0:
        return "long-dec-short-hl", long_dec
    return None, short_dec


def dec_funding_hourly(row):
    """Match hedge.py when funding_period_s is 3600; scale other periods to hourly."""
    bps = float(row.get("funding_rate_bps") or 0)
    sign = 1.0 if row.get("is_funding_positive") else -1.0
    rate = bps / 1e4 * sign
    period = float(row.get("funding_period_s") or 3600)
    if period <= 0:
        period = 3600.0
    return rate * (3600.0 / period)


def adverse_basis_usd(direction, dec_mid, hl_mid, size, buffer_bps):
    """Cost of an adverse mid-to-mid gap, plus a small buffer. Favorable gaps are not credited."""
    if direction == "short-dec-long-hl":
        signed = (dec_mid - hl_mid) * size
    else:
        signed = (hl_mid - dec_mid) * size
    mid = (dec_mid + hl_mid) / 2.0 if dec_mid and hl_mid else (dec_mid or hl_mid or 0)
    buffer = (buffer_bps / 1e4) * abs(size) * mid
    return max(0.0, -signed) + buffer


def expected_usd(net_hr, notional, maker_fee, legs, adverse):
    return net_hr * notional * 168.0 - legs * maker_fee * notional - adverse


def floor_lot(size, lot):
    if lot <= 0:
        return 0.0
    return round(math.floor(size / lot + 1e-12) * lot, 8)


def entry_size(price, *, lot, min_size, coin, dec_oi, cfg, dec_equity, hl_free, hl_mtm,
               existing_dec_notional, existing_hl_notional, existing_total):
    if price <= 0 or lot <= 0:
        return 0.0
    risk = cfg["risk"]
    entry = cfg["entry"]
    max_lev = risk["max_leverage"]
    dec_budget = max_lev * dec_equity - existing_dec_notional
    hl_budget = max_lev * hl_free - existing_hl_notional
    account_room = min(
        risk["max_total_notional_usd"] - existing_total,
        max_lev * dec_equity - existing_total,
        max_lev * hl_mtm - existing_total,
    )
    oi_cap = entry["max_position_oi_fraction"] * dec_oi * price
    notional = min(risk["max_pair_notional_usd"], dec_budget, hl_budget, account_room, oi_cap)
    if notional < cfg["hl_min_notional_usd"]:
        return 0.0
    size = floor_lot(notional / price, lot)
    if coin == "BTC":
        size = min(size, cfg["max_size_btc"])
        size = floor_lot(size, lot)
    if size < min_size:
        return 0.0
    if size * price < cfg["hl_min_notional_usd"]:
        return 0.0
    if size > entry["max_position_oi_fraction"] * dec_oi + 1e-12:
        return 0.0
    return size


def liq_distance(mark, liq):
    if not mark or mark <= 0 or liq is None:
        return None
    try:
        liq_f = float(liq)
    except (TypeError, ValueError):
        return None
    if liq_f <= 0:
        return None
    return abs(liq_f - mark) / mark


def flat_enough(dec, hl, price, cfg):
    if abs(dec) <= 1e-12 and abs(hl) <= 1e-12:
        return True
    # One venue only is a naked leg even when the notional is under the HL minimum.
    if abs(dec) <= 1e-12 or abs(hl) <= 1e-12:
        return False
    gap = abs(dec + hl)
    gross = max(abs(dec), abs(hl))
    if gross > 0 and gap <= cfg["delta_tolerance_fraction"] * gross + 1e-12:
        return True
    if price > 0 and gap * price <= cfg["delta_tolerance_min_notional_usd"]:
        return True
    return False


def held_direction(dec, hl):
    if dec < 0 and hl > 0:
        return "short-dec-long-hl"
    if dec > 0 and hl < 0:
        return "long-dec-short-hl"
    return None


def repair_kind(dec, hl, price, cfg, lot, min_size):
    if flat_enough(dec, hl, price, cfg):
        return None
    if dec * hl > 0:
        return "close"
    if abs(hl) > abs(dec):
        need = abs(hl) - abs(dec)
        if need + 1e-12 < max(min_size, lot):
            return "close"
        return "hedge-dec"
    need = abs(dec) - abs(hl)
    if price <= 0 or need * price < cfg["hl_min_notional_usd"] * 1.1:
        return "close"
    return "hedge-hl"


def consecutive_negative(rows, direction):
    streak = 0
    for row in reversed(rows):
        if hourly_net(direction, row["dec"], row["hl"]) < 0:
            streak += 1
        else:
            break
    return streak


def snap_price(snap):
    vals = [v for v in (snap.get("dec_mid"), snap.get("hl_mid"), snap.get("dec_mark"), snap.get("hl_mark")) if v and v > 0]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def gross_base(snap):
    return max(abs(snap.get("dec_sz") or 0), abs(snap.get("hl_sz") or 0))


def roll_day(state, equity, today, stop):
    """Return (loss_triggered, day, day_start_equity). A new UTC day resets the baseline."""
    if equity is None:
        return False, state.get("day"), state.get("day_start_equity")
    start = state.get("day_start_equity")
    if state.get("day") != today or not isinstance(start, (int, float)):
        return False, today, equity
    return equity <= float(start) - stop, today, float(start)


def _window(history, coin, window):
    rows = [r for r in history if r["coin"] == coin]
    rows.sort(key=lambda r: r["hour"])
    return rows[-window:]


def _action(kind, coin, reason, **extra):
    item = {"kind": kind, "coin": coin, "reason": reason}
    item.update(extra)
    return item


def entry_candidate(snap, rows, cfg, account, existing):
    entry = cfg["entry"]
    if not snap.get("tradable"):
        return None
    if not snap.get("in_overlap"):
        return None
    if len(rows) < entry["min_snapshots"]:
        return None
    price = snap_price(snap)
    if price <= 0:
        return None
    direction, _ = choose_direction(snap["dec_funding_hr"], snap["hl_funding_hr"])
    if not direction:
        return None
    current = hourly_net(direction, snap["dec_funding_hr"], snap["hl_funding_hr"])
    if entry.get("require_current_hour_positive") and current <= 0:
        return None
    avg = sum(hourly_net(direction, r["dec"], r["hl"]) for r in rows) / len(rows)
    if avg <= 0:
        return None
    dec_oi = snap.get("dec_oi") or 0
    hl_oi = snap.get("hl_oi") or 0
    if dec_oi * price < entry["min_decibel_oi_usd"]:
        return None
    if hl_oi * price < entry["min_hl_oi_usd"]:
        return None
    size = entry_size(
        price,
        lot=snap["lot"],
        min_size=snap["min_size"],
        coin=snap["coin"],
        dec_oi=dec_oi,
        cfg=cfg,
        dec_equity=account["dec_equity"],
        hl_free=account["hl_free"],
        hl_mtm=account["hl_mtm"],
        existing_dec_notional=existing["dec"],
        existing_hl_notional=existing["hl"],
        existing_total=existing["total"],
    )
    if size <= 0:
        return None
    notional = size * price
    adverse = adverse_basis_usd(direction, snap["dec_mid"], snap["hl_mid"], size, entry["basis_buffer_bps"])
    edge = expected_usd(avg, notional, cfg["maker_fee"], 4, adverse)
    if edge <= entry["min_7d_net_usd"]:
        return None
    return {
        "coin": snap["coin"],
        "direction": direction,
        "size": size,
        "edge": edge,
        "notional": notional,
        "detail": f"7d net ${edge:.2f} after maker fees and adverse basis, {len(rows)}h avg",
    }


def decide(snaps, account, cfg, history, state, now):
    """Pure entry/exit/repair decision. `history` rows use keys coin, hour, dec, hl."""
    actions = []
    errors = list(account.get("errors") or [])
    window = cfg["entry"]["snapshot_window"]
    exit_cfg = cfg["exit"]
    floor = cfg["risk"]["liq_distance_floor"]
    positioned = [
        s for s in snaps
        if s.get("size_ok", True) and (abs(s.get("dec_sz") or 0) > 1e-12 or abs(s.get("hl_sz") or 0) > 1e-12)
    ]

    def notionals():
        dec = hl = total = 0.0
        for s in positioned:
            px = snap_price(s)
            dec += abs(s.get("dec_sz") or 0) * px
            hl += abs(s.get("hl_sz") or 0) * px
            total += gross_base(s) * px
        return {"dec": dec, "hl": hl, "total": total}

    existing = notionals()

    if account.get("daily_loss"):
        for s in positioned:
            actions.append(_action("exit", s["coin"], "daily-loss", detail="UTC-day loss stop"))
    else:
        closed = set()
        for s in positioned:
            dists = []
            for mark, liq, venue in (
                (s.get("dec_mark"), s.get("dec_liq"), "Decibel"),
                (s.get("hl_mark"), s.get("hl_liq"), "Hyperliquid"),
            ):
                dist = liq_distance(mark, liq)
                if dist is not None:
                    dists.append((dist, venue))
            if dists and min(d for d, _ in dists) < floor:
                worst, venue = min(dists)
                actions.append(_action(
                    "exit", s["coin"], "liq",
                    detail=f"{venue} liquidation distance {worst * 100:.1f}% < {floor * 100:.0f}%",
                ))
                closed.add(s["coin"])

        if account.get("equity_ok"):
            limit = min(
                cfg["risk"]["max_total_notional_usd"],
                cfg["risk"]["max_leverage"] * account["dec_equity"],
                cfg["risk"]["max_leverage"] * account["hl_mtm"],
            )
            ranked = sorted(positioned, key=lambda s: gross_base(s) * snap_price(s), reverse=True)
            total = existing["total"]
            for s in ranked:
                if total <= limit + 1.0:
                    break
                if s["coin"] in closed:
                    total -= gross_base(s) * snap_price(s)
                    continue
                actions.append(_action(
                    "exit", s["coin"], "leverage",
                    detail=f"book notional ${total:.0f} above ${limit:.0f} leverage/total cap",
                ))
                closed.add(s["coin"])
                total -= gross_base(s) * snap_price(s)

        for s in positioned:
            if s["coin"] in closed:
                continue
            price = snap_price(s)
            if not s.get("in_overlap"):
                actions.append(_action("exit", s["coin"], "no-overlap", detail="position has no matching perp on the other venue"))
                continue
            if not flat_enough(s.get("dec_sz") or 0, s.get("hl_sz") or 0, price, cfg):
                how = repair_kind(s.get("dec_sz") or 0, s.get("hl_sz") or 0, price, cfg, s.get("lot") or 1e-8, s.get("min_size") or 0)
                actions.append(_action("repair", s["coin"], how or "close", how=how or "close", detail="naked or mismatched legs"))
                continue
            direction = held_direction(s.get("dec_sz") or 0, s.get("hl_sz") or 0)
            if not direction:
                continue
            rows = _window(history, s["coin"], window)
            if len(rows) < exit_cfg["min_snapshots"]:
                continue
            avg = sum(hourly_net(direction, r["dec"], r["hl"]) for r in rows) / len(rows)
            notional = gross_base(s) * price
            edge = expected_usd(avg, notional, cfg["maker_fee"], 2, 0.0)
            streak = consecutive_negative(rows, direction)
            if edge < exit_cfg["min_7d_net_usd"]:
                actions.append(_action("exit", s["coin"], "decay", direction=direction, detail=f"forward 7d ${edge:.2f} after close fees"))
            elif streak >= exit_cfg["flip_hours"]:
                actions.append(_action("exit", s["coin"], "flip", direction=direction, detail=f"negative carry for {streak}h"))

    risk_busy = any(a["kind"] in ("exit", "repair") for a in actions)
    open_coins = {s["coin"] for s in positioned}
    slots = cfg["risk"]["max_concurrent_pairs"] - len(open_coins)
    candidates = []
    if account.get("equity_ok"):
        running = dict(existing)
        for snap in snaps:
            if snap["coin"] in open_coins or not snap.get("size_ok", True):
                continue
            rows = _window(history, snap["coin"], window)
            cand = entry_candidate(snap, rows, cfg, account, running)
            if cand:
                candidates.append((cand, snap))
        candidates.sort(key=lambda item: item[0]["edge"], reverse=True)
    taken = 0
    paused_entries = account.get("paused")
    for cand, _snap in candidates:
        can_enter = (
            not paused_entries
            and not risk_busy
            and slots > 0
            and taken < cfg["entry"]["max_new_entries_per_run"]
        )
        if can_enter:
            actions.append(_action(
                "enter", cand["coin"], "funding",
                direction=cand["direction"], size=cand["size"], detail=cand["detail"],
            ))
            taken += 1
            slots -= 1
        else:
            reason = "paused" if paused_entries else ("deferred" if risk_busy else "capacity")
            actions.append(_action(
                "signal", cand["coin"], reason,
                direction=cand["direction"], size=cand["size"], detail=cand["detail"],
            ))

    comment, alert_keys = render_alerts(actions, errors, state, now, cfg, account.get("paper", True))
    new_state = {
        "positions": {},
        "day": account.get("day"),
        "day_start_equity": account.get("day_start_equity"),
        "alert_keys": alert_keys,
    }
    for s in positioned:
        prev = (state.get("positions") or {}).get(s["coin"]) or {}
        direction = held_direction(s.get("dec_sz") or 0, s.get("hl_sz") or 0) or prev.get("direction")
        entry_net = prev.get("entry_net_hr")
        adopted = prev.get("adopted", True)
        if not prev and direction is not None:
            entry_net = hourly_net(direction, s.get("dec_funding_hr") or 0, s.get("hl_funding_hr") or 0)
            adopted = True
        new_state["positions"][s["coin"]] = {
            "direction": direction,
            "dec_sz": s.get("dec_sz") or 0,
            "hl_sz": s.get("hl_sz") or 0,
            "adopted": adopted,
            "since": prev.get("since") or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entry_net_hr": entry_net,
        }
    return {"actions": actions, "errors": errors, "comment": comment, "state": new_state}


def render_alerts(actions, errors, state, now, cfg, paper):
    prev = dict((state or {}).get("alert_keys") or {})
    realert_h = cfg.get("risk_realert_hours", 6)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    posted = []
    new_keys = {}

    def age_hours(stamp):
        try:
            then = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return 1e9
        return (now - then).total_seconds() / 3600.0

    for action in actions:
        key = _alert_key(action)
        force = (not paper) and action["kind"] in ("enter", "exit", "repair")
        if key is None:
            posted.append(action)
            continue
        if force or key not in prev or (action["kind"] in ("exit", "repair") and age_hours(prev.get(key)) >= realert_h):
            posted.append(action)
            new_keys[key] = now_iso
        else:
            new_keys[key] = prev[key]
    for key in list(prev):
        if key.startswith("risk:") and key not in new_keys:
            _risk, coin, reason = (key.split(":", 2) + ["", ""])[:3]
            posted.append(_action("clear", coin, reason, detail="condition cleared"))
    for err in errors:
        posted.append(_action("error", "", "error", detail=err))

    if not posted:
        return None, new_keys
    mode = "PAPER — no orders" if paper else "LIVE"
    lines = [f"**Hedge** ({mode}) {now_iso}", ""]
    for action in posted[:40]:
        lines.append("- " + _fmt_action(action))
    if len(posted) > 40:
        lines.append(f"- … {len(posted) - 40} more")
    return "\n".join(lines), new_keys


def _alert_key(action):
    if action["kind"] in ("enter", "signal"):
        return f"signal:{action['coin']}:{action.get('direction')}"
    if action["kind"] in ("exit", "repair"):
        return f"risk:{action['coin']}:{action['reason']}"
    return None


def _fmt_action(action):
    kind = action["kind"].upper()
    coin = action.get("coin") or ""
    reason = action.get("reason") or ""
    extra = action.get("detail") or ""
    direction = action.get("direction") or ""
    size = action.get("size")
    size_s = f" size {size:.6g}" if isinstance(size, float) else ""
    dir_s = f" {direction}" if direction else ""
    return f"{kind} {coin}{dir_s}{size_s} ({reason}) {extra}".strip()


def load_history(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="") as f:
        for raw in csv.DictReader(f):
            rows.append({
                "hour": raw["hour"],
                "coin": raw["coin"],
                "dec": float(raw["dec_funding_hr"]),
                "hl": float(raw["hl_funding_hr"]),
                "dec_oi_base": float(raw.get("dec_oi_base") or 0),
                "hl_oi_base": float(raw.get("hl_oi_base") or 0),
                "dec_mid": float(raw.get("dec_mid") or 0),
                "hl_mid": float(raw.get("hl_mid") or 0),
                "dec_mark": float(raw.get("dec_mark") or 0),
                "hl_mark": float(raw.get("hl_mark") or 0),
            })
    return rows


def write_history(path, rows):
    rows = sorted(rows, key=lambda r: (r["hour"], r["coin"]))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "hour": row["hour"],
                "coin": row["coin"],
                "dec_funding_hr": row["dec"] if "dec" in row else row["dec_funding_hr"],
                "hl_funding_hr": row["hl"] if "hl" in row else row["hl_funding_hr"],
                "dec_oi_base": row.get("dec_oi_base", 0),
                "hl_oi_base": row.get("hl_oi_base", 0),
                "dec_mid": row.get("dec_mid", 0),
                "hl_mid": row.get("hl_mid", 0),
                "dec_mark": row.get("dec_mark", 0),
                "hl_mark": row.get("hl_mark", 0),
            })


def prune_history(rows, now, days=90):
    cutoff = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H")
    return [r for r in rows if r["hour"] >= cutoff]


def upsert_hour(rows, hour, snaps):
    keep = [r for r in rows if not (r["hour"] == hour)]
    # Keep other hours intact, replace this hour entirely so a delisted name doesn't linger in the bucket.
    for snap in snaps:
        if not snap.get("in_overlap"):
            continue
        if snap.get("dec_funding_hr") is None or snap.get("hl_funding_hr") is None:
            continue
        keep.append({
            "hour": hour,
            "coin": snap["coin"],
            "dec": snap["dec_funding_hr"],
            "hl": snap["hl_funding_hr"],
            "dec_oi_base": snap.get("dec_oi") or 0,
            "hl_oi_base": snap.get("hl_oi") or 0,
            "dec_mid": snap.get("dec_mid") or 0,
            "hl_mid": snap.get("hl_mid") or 0,
            "dec_mark": snap.get("dec_mark") or 0,
            "hl_mark": snap.get("hl_mark") or 0,
        })
    return keep


def load_state(path):
    if not os.path.exists(path):
        return {"positions": {}, "alert_keys": {}}
    with open(path) as f:
        data = json.load(f)
    data.setdefault("positions", {})
    data.setdefault("alert_keys", {})
    return data


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")


def _http(method, url, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read()[:160].decode("utf-8", "replace")
        path = urllib.request.urlparse(url).path
        raise RuntimeError(f"HTTP {e.code} {path} {sanitize(body)}") from None


def _bearer():
    key = os.environ.get("GEOMI_CLIENT_KEY") or ""
    if not key:
        raise RuntimeError("GEOMI_CLIENT_KEY not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def chain_signed(subaccount, market_addr, sz_decimals):
    def view(fn):
        payload = {
            "function": f"{DEC_PKG}::perp_engine::{fn}",
            "type_arguments": [],
            "arguments": [subaccount, market_addr],
        }
        return _http("POST", f"{APTOS_NODE}/view", payload, {"Content-Type": "application/json"})
    if not view("has_position")[0]:
        return 0.0
    size = int(view("get_position_size")[0]) / (10 ** sz_decimals)
    is_long = bool(view("get_position_is_long")[0])
    return size if is_long else -size


def fetch_hl_meta():
    meta, ctxs = _http("POST", HL_INFO, {"type": "metaAndAssetCtxs"}, {"Content-Type": "application/json"})
    out = {}
    for uni, ctx in zip(meta["universe"], ctxs):
        out[uni["name"].upper()] = {
            "name": uni["name"],
            "sz_decimals": int(uni["szDecimals"]),
            "funding": float(ctx["funding"]),
            "oi": float(ctx["openInterest"]),
            "mark": float(ctx["markPx"]),
            "mid": float(ctx.get("midPx") or ctx["markPx"]),
        }
    return out


def fetch_hl_account(master):
    state = _http("POST", HL_INFO, {"type": "clearinghouseState", "user": master}, {"Content-Type": "application/json"})
    spot = _http("POST", HL_INFO, {"type": "spotClearinghouseState", "user": master}, {"Content-Type": "application/json"})
    positions = {}
    upnl = 0.0
    for ap in state.get("assetPositions") or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        if not coin:
            continue
        sz = float(pos.get("szi") or 0)
        upnl += float(pos.get("unrealizedPnl") or 0)
        liq = pos.get("liquidationPx")
        positions[coin.upper()] = {"sz": sz, "liq": float(liq) if liq not in (None, "", "None") else None}
    usdc_total = usdc_hold = 0.0
    for bal in spot.get("balances") or []:
        if bal.get("coin") == "USDC":
            usdc_total = float(bal.get("total") or 0)
            usdc_hold = float(bal.get("hold") or 0)
    return {
        "positions": positions,
        "upnl": upnl,
        "usdc_total": usdc_total,
        "usdc_free": usdc_total - usdc_hold,
        "mtm": usdc_total + upnl,
    }


def fetch_dec_markets():
    rows = _http("GET", f"{DEC_API}/markets", headers=_bearer())
    if isinstance(rows, dict):
        rows = rows.get("markets") or rows.get("items") or []
    out = []
    for row in rows:
        if row.get("asset_type") == "spot":
            continue
        name = str(row.get("market_name") or "")
        base = name.split("/")[0].split("-")[0].upper()
        if not base:
            continue
        out.append(row | {"base": base})
    return out


def fetch_dec_prices():
    rows = _http("GET", f"{DEC_API}/prices", headers=_bearer())
    if isinstance(rows, dict):
        rows = rows.get("prices") or rows.get("items") or []
    return {str(row.get("market") or "").lower(): row for row in rows}


def fetch_dec_positions(subaccount):
    url = f"{DEC_API}/account_positions?account={subaccount}&include_deleted=false&limit=100"
    rows = _http("GET", url, headers=_bearer())
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("positions") or []
    return [r for r in rows if not r.get("is_deleted")]


def fetch_dec_overview(subaccount):
    url = f"{DEC_API}/account_overviews?account={subaccount}"
    data = _http("GET", url, headers=_bearer())
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def base_symbol(name):
    return str(name or "").split("/")[0].split("-")[0].upper()


def build_snaps(markets, prices, hl, dec_positions):
    by_addr = {}
    snaps = []
    for market in markets:
        base = market["base"]
        hl_row = hl.get(base)
        if not hl_row:
            continue
        addr = str(market.get("market_addr") or "")
        px = prices.get(addr.lower()) or {}
        spec = {
            "coin": hl_row["name"],
            "dec_name": market.get("market_name") or f"{base}/USD",
            "dec_addr": addr,
            "sz_decimals": int(market.get("sz_decimals") or 0),
            "px_decimals": int(market.get("px_decimals") or 0),
            "tick": float(market.get("tick_size") or 0),
            "lot": float(market.get("lot_size") or 0),
            "min_size": float(market.get("min_size") or 0),
            "hl_sz_decimals": hl_row["sz_decimals"],
        }
        if base == "BTC" and addr.lower() == BTC_MARKET.lower():
            spec.update({k: BTC_SPEC[k] for k in ("tick", "lot", "min_size", "sz_decimals", "px_decimals", "hl_sz_decimals")})
            spec["certified_btc"] = True
        mode = str(market.get("mode") or "Open")
        tradable = mode == "Open" and spec["tick"] > 0 and spec["lot"] > 0 and spec["min_size"] > 0
        snap = {
            "coin": hl_row["name"],
            "spec": spec,
            "in_overlap": True,
            "tradable": tradable,
            "mode": mode,
            "dec_sz": 0.0,
            "hl_sz": 0.0,
            "dec_funding_hr": dec_funding_hourly(px) if px else None,
            "hl_funding_hr": hl_row["funding"],
            "dec_oi": float(px.get("open_interest") or 0) if px else 0.0,
            "hl_oi": hl_row["oi"],
            "dec_mid": float(px.get("mid_px") or 0) if px else 0.0,
            "hl_mid": hl_row["mid"],
            "dec_mark": float(px.get("mark_px") or 0) if px else 0.0,
            "hl_mark": hl_row["mark"],
            "dec_liq": None,
            "hl_liq": None,
            "lot": spec["lot"] or BTC_SPEC["lot"],
            "min_size": spec["min_size"] or BTC_SPEC["min_size"],
        }
        snaps.append(snap)
        by_addr[addr.lower()] = snap

    for pos in dec_positions:
        addr = str(pos.get("market") or "").lower()
        snap = by_addr.get(addr)
        if snap is None:
            continue
        # Provisional. confirm_chain_sizes overwrites this with the signed chain view.
        try:
            snap["dec_sz"] = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            snap["dec_sz"] = 0.0
        liq = pos.get("estimated_liquidation_price")
        snap["dec_liq"] = float(liq) if liq not in (None, "", 0, 0.0) else None
    return snaps, by_addr


def confirm_chain_sizes(snaps, subaccount):
    """Chain view is the signed-size source of truth for BTC and any coin with a live leg."""
    notes = []
    for snap in snaps:
        spec = snap.get("spec")
        if not spec:
            continue
        interesting = (
            snap["coin"] == "BTC"
            or abs(snap.get("hl_sz") or 0) > 0
            or abs(snap.get("dec_sz") or 0) > 0
        )
        if not interesting:
            continue
        try:
            snap["dec_sz"] = chain_signed(subaccount, spec["dec_addr"], spec["sz_decimals"])
            snap["size_ok"] = True
        except RuntimeError as e:
            snap["size_ok"] = False
            notes.append(sanitize(f"{snap['coin']} chain size unreadable: {e}")[:180])
    return notes


def attach_hl_positions(snaps, hl_account, hl_meta):
    known = {s["coin"].upper(): s for s in snaps}
    for coin, pos in hl_account["positions"].items():
        snap = known.get(coin)
        if snap is None and abs(pos["sz"]) > 0:
            snap = {
                "coin": coin,
                "spec": None,
                "in_overlap": False,
                "tradable": False,
                "dec_sz": 0.0,
                "hl_sz": pos["sz"],
                "dec_funding_hr": None,
                "hl_funding_hr": None,
                "dec_oi": 0.0,
                "hl_oi": 0.0,
                "dec_mid": 0.0,
                "hl_mid": 0.0,
                "dec_mark": 0.0,
                "hl_mark": (hl_meta.get(coin) or {}).get("mark") or 0.0,
                "dec_liq": None,
                "hl_liq": pos["liq"],
                "lot": 1e-8,
                "min_size": 0.0,
            }
            snaps.append(snap)
            known[coin] = snap
        elif snap is not None:
            snap["hl_sz"] = pos["sz"]
            snap["hl_liq"] = pos["liq"]
    return snaps


def address_mismatches(cfg):
    notes = []
    sub = (os.environ.get("DECIBEL_SUBACCOUNT") or "").strip()
    if sub and sub.lower() != cfg["decibel_subaccount"].lower():
        notes.append("DECIBEL_SUBACCOUNT secret does not match bot/config.json decibel_subaccount; this bot uses the config address")
    master = (os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS") or "").strip()
    if master and master.lower() != cfg["hl_master"].lower():
        notes.append("HYPERLIQUID_ACCOUNT_ADDRESS secret does not match bot/config.json hl_master; this bot uses the config address")
    return notes


def execute_live(actions, snaps):
    import hedge
    by_coin = {s["coin"]: s for s in snaps}
    failures = []
    for action in actions:
        if action["kind"] not in ("enter", "exit", "repair"):
            continue
        snap = by_coin.get(action["coin"])
        if snap is None or not snap.get("spec"):
            failures.append(f"{action['coin']}: no market spec, skipped {action['kind']}")
            continue
        spec = snap["spec"]
        hedge.set_market(
            spec["coin"], spec["dec_name"], spec["dec_addr"], spec["sz_decimals"], spec["px_decimals"],
            spec["tick"], spec["lot"], spec["min_size"], spec["hl_sz_decimals"],
        )
        cfg = load_config()
        ns = argparse.Namespace(
            size=float(action.get("size") or 0),
            direction=action.get("direction") or "short-dec-long-hl",
            live=True, yes=True, dry_run=False,
            maker_timeout=cfg["maker_timeout_s"], hedge_timeout=cfg["hedge_timeout_s"],
            max_slip_bps=cfg["max_slip_bps"], poll=cfg["poll_s"],
            set_leverage=action["kind"] == "enter",
            leverage=int(cfg["risk"]["max_leverage"]),
            force=False, allow_existing=action["kind"] != "enter",
            hold_hl=action.get("how") == "hedge-dec",
            skip_funding_check=action["kind"] == "repair",
            ignore_pause=False, coin=spec["coin"], show_balances=False,
        )
        try:
            if action["kind"] == "enter":
                hedge.cmd_open(ns)
            elif action["kind"] == "exit" or action.get("how") == "close" or action["reason"] in ("close", "daily-loss", "liq", "leverage", "decay", "flip", "no-overlap"):
                hedge.cmd_close(ns)
            elif action.get("how") == "hedge-hl":
                hedge.run_hedge("sync", ns, None)
            elif action.get("how") == "hedge-dec":
                dec, hl = snap.get("dec_sz") or 0, snap.get("hl_sz") or 0
                need = abs(hl) - abs(dec)
                ns.size = floor_lot(need, spec["lot"])
                ns.direction = "short-dec-long-hl" if hl > 0 else "long-dec-short-hl"
                ns.allow_existing = True
                ns.hold_hl = True
                ns.skip_funding_check = True
                hedge.cmd_open(ns)
            else:
                hedge.cmd_close(ns)
        except SystemExit as e:
            if e.code not in (0, None):
                failures.append(f"{action['coin']} {action['kind']} failed")
        except Exception as e:
            failures.append(sanitize(f"{action['coin']} {action['kind']} {e}")[:240])
    return failures


def net_hedge_status(net, tolerance=0.0002):
    """0 when the two BTC legs offset. 1 when the book is unhedged."""
    return 0 if abs(float(net)) < tolerance else 1


def public_check(cfg):
    """Read-only BTC leg check using public Hyperliquid and Aptos views. No Geomi key, no orders."""
    hl = fetch_hl_account(cfg["hl_master"])
    hl_sz = (hl["positions"].get("BTC") or {}).get("sz") or 0.0
    dec_sz = chain_signed(cfg["decibel_subaccount"], BTC_MARKET, 8)
    net = dec_sz + hl_sz
    print(f"public-check coin=BTC dec={dec_sz:+.5f} hl={hl_sz:+.5f} net={net:+.5f}")
    return net_hedge_status(net)


def run(data_dir):
    cfg = load_config()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    hour = now.strftime("%Y-%m-%dT%H")
    today = now.strftime("%Y-%m-%d")
    os.makedirs(data_dir, exist_ok=True)
    hist_path = os.path.join(data_dir, "funding_snapshots.csv")
    state_path = os.path.join(data_dir, "state.json")
    decisions_path = os.path.join(data_dir, "decisions.jsonl")
    state = load_state(state_path)
    history = load_history(hist_path)
    errors = [sanitize(n) for n in address_mismatches(cfg)]
    is_paper = paper_mode()
    is_paused = paused()

    try:
        markets = fetch_dec_markets()
        prices = fetch_dec_prices()
        positions = fetch_dec_positions(cfg["decibel_subaccount"])
        overview = fetch_dec_overview(cfg["decibel_subaccount"])
        hl_meta = fetch_hl_meta()
        hl_acct = fetch_hl_account(cfg["hl_master"])
    except Exception as e:
        errors.append(sanitize(e)[:300])
        _finish(data_dir, hist_path, state_path, decisions_path, history, state, errors, now, is_paper, None)
        print(f"hedge-monitor pairs=0 positions=0 alerts=1 paper={int(is_paper)} paused={int(is_paused)} errors={len(errors)}")
        return 1

    snaps, _by_addr = build_snaps(markets, prices, hl_meta, positions)
    snaps = attach_hl_positions(snaps, hl_acct, hl_meta)
    errors.extend(confirm_chain_sizes(snaps, cfg["decibel_subaccount"]))
    history = prune_history(upsert_hour(history, hour, snaps), now)
    # The decision sees the snapshot just recorded.
    hist_view = [
        {"hour": r["hour"], "coin": r["coin"], "dec": r["dec"], "hl": r["hl"]}
        for r in history
    ]
    dec_equity = overview.get("perp_equity_balance")
    try:
        dec_equity = float(dec_equity)
    except (TypeError, ValueError):
        dec_equity = None
    hl_free = hl_acct["usdc_free"]
    hl_mtm = hl_acct["mtm"]
    equity_ok = dec_equity is not None and dec_equity >= 10 and hl_mtm >= 10 and hl_free >= 0
    if not equity_ok:
        errors.append("venue equity unreadable or below $10; no new entries and no leverage stop")
    total_equity = (dec_equity + hl_mtm) if equity_ok else None
    loss, day, day_start = roll_day(state, total_equity, today, cfg["risk"]["daily_loss_stop_usd"])
    account = {
        "dec_equity": dec_equity,
        "hl_free": hl_free,
        "hl_mtm": hl_mtm,
        "equity_ok": equity_ok,
        "daily_loss": loss,
        "paused": is_paused,
        "paper": is_paper,
        "errors": errors,
        "day": day,
        "day_start_equity": day_start,
    }
    result = decide(snaps, account, cfg, hist_view, state, now)
    if not is_paper:
        # Risk actions first. Skip entries in this run if any of those were requested;
        # decide() already withholds enter actions in that case.
        failures = execute_live(result["actions"], snaps)
        for failure in failures:
            result["errors"].append(failure)
            result["comment"] = (result["comment"] or f"**Hedge** (LIVE) {hour}") + f"\n- ERROR {failure}"
    _finish(data_dir, hist_path, state_path, decisions_path, history, result["state"], result["errors"], now, is_paper, result)
    open_n = sum(1 for s in snaps if abs(s.get("dec_sz") or 0) > 0 or abs(s.get("hl_sz") or 0) > 0)
    print(
        f"hedge-monitor pairs={sum(1 for s in snaps if s.get('in_overlap'))} "
        f"positions={open_n} alerts={0 if not result['comment'] else 1} "
        f"paper={int(is_paper)} paused={int(is_paused)} errors={len(result['errors'])}"
    )
    _step_summary(result, is_paper, is_paused, snaps)
    return 1 if result["errors"] else 0


def _finish(data_dir, hist_path, state_path, decisions_path, history, state, errors, now, is_paper, result):
    try:
        write_history(hist_path, history)
        write_json(state_path, state)
        logged = [
            {k: v for k, v in a.items() if k in ("kind", "coin", "reason", "direction", "size", "detail")}
            for a in (result or {}).get("actions") or []
            if a.get("kind") != "signal"
        ]
        if logged or errors:
            append_jsonl(decisions_path, {
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "paper": is_paper,
                "actions": logged,
                "errors": errors,
            })
    except OSError as e:
        errors.append(sanitize(e)[:200])
    comment = None if result is None else result.get("comment")
    if errors and result is None:
        comment = "**Hedge error**\n" + "\n".join(f"- {e}" for e in errors)
    status = comment_if_needed(comment)
    if status == "failed" and comment:
        errors.append("failed to comment on the Hedge alerts issue")
    for err in errors:
        print("hedge-error:", sanitize(err)[:300])


def _step_summary(result, is_paper, is_paused, snaps):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"Mode: {'paper' if is_paper else 'LIVE'}",
        f"Entries paused: {str(is_paused).lower()}",
        f"Overlapping pairs: {sum(1 for s in snaps if s.get('in_overlap'))}",
    ]
    for snap in snaps:
        if abs(snap.get("dec_sz") or 0) > 0 or abs(snap.get("hl_sz") or 0) > 0:
            lines.append(f"Position {snap['coin']}: dec {snap.get('dec_sz'):+.5f} hl {snap.get('hl_sz'):+.5f}")
    if result.get("comment"):
        lines.append("Alert comment posted.")
    else:
        lines.append("No alert.")
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["run", "public-check"])
    ap.add_argument("--data-dir", default=os.path.join(HERE, "runtime"))
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "public-check":
        sys.exit(public_check(cfg))
    sys.exit(run(args.data_dir))


if __name__ == "__main__":
    main()
