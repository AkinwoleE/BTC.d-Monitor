#!/usr/bin/env python3
"""
Sizing, schedule, and order plan for the ASTER unlock short.

This module is pure: it does not read credentials, talk to Hyperliquid, or
place orders. aster_unlock_short.py is the only place that can submit anything,
and only when the repo variable ASTER_LIVE is exactly "true" (scheduled runs)
or that variable is "true" and a workflow_dispatch sets dry_run to false.

Plan A (approved):
  - Short the ASTER perp, isolated margin, 2x leverage (market max is 5x).
  - Risk 10% of account value as margin. Notional cap = that margin x 2.
  - Three equal tranches, due 2026-10-29, 2026-11-01, and 2026-11-03 at 12:00 UTC.
  - Take profit with fixed-size reduce-only trigger buys: half at 0.665, the
    rest at 0.63. Refreshed from the live position after each fill.
  - Invalidate the trade when a closed 4h candle's close is above 0.785.
  - Native reduce-only stop-market backstop at 0.835, not 0.82. On the 4h
    candle that opened 2026-09-05 16:00 UTC, ASTER wicked to 0.82049 and
    closed at 0.78479, so a 0.82 stop would have fired without a 4h close
    above 0.785. Across the following 120 days no 4h candle closed at or
    below 0.785 while trading at or above 0.835. 0.835 is also well under
    the isolated 2x liquidation estimate at recent prices (~1.36x entry).
  - Flat by 2026-11-05 12:00 UTC.

Partial take-profits are standalone reduce-only triggers (grouping "na").
Hyperliquid's positionTpsl grouping is sized off the whole position, so it
cannot express "half at 0.665, half at 0.63". normalTpsl children are tied
to a parent order and are cancelled if that parent is an IOC that only
partially fills, so the entry is sent alone and the triggers are placed
only after a confirmed fill.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

SYMBOL = "ASTER"
LEVERAGE = 2
MARGIN_FRACTION = 0.10
N_TRANCHES = 3
# Leave a little free margin for the taker fee so the IOC is not rejected.
MARGIN_BUFFER = 0.99
MIN_ORDER_NOTIONAL_USD = 10.0  # Hyperliquid perp minimum order value
ENTRY_SLIPPAGE = 0.02          # IOC sell limit vs mid; unfilled size is not consumed
TRIGGER_SLIPPAGE = 0.10        # native market TP/SL slippage tolerance
EXIT_SLIPPAGE = 0.10

TP1_PX = 0.665
TP2_PX = 0.63
CLOSE_STOP_PX = 0.785
SL_BACKSTOP_PX = 0.835

TRANCHES = (
    ("2026-10-29", datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)),
    ("2026-11-01", datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)),
    ("2026-11-03", datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)),
)
TIME_EXIT = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
# Scheduled management continues a couple of days past the time exit so a
# delayed Actions run can still flatten a leftover position. Entries stop
# at TIME_EXIT regardless.
MANAGE_UNTIL = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
CAMPAIGN_START = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)

ENTRY_CRONS = frozenset({
    "5 12 29 10 *",
    "5 16 29 10 *",
    "5 12 1 11 *",
    "5 16 1 11 *",
    "5 12 3 11 *",
    "5 16 3 11 *",
})
EXIT_CRONS = frozenset({
    "5 12 5 11 *",
    "5 16 5 11 *",
})
STOP_CRON = "10 0,4,8,12,16,20 * * *"
ACTIONS = ("status", "enter_tranche", "check_stop", "close_all")
TRANCHE_DATES = {due.date() for _tid, due in TRANCHES}


def empty_state():
    return {"version": 1, "symbol": SYMBOL, "tranches": {}, "exit": None}


def validate_state(state):
    if not isinstance(state, dict):
        raise ValueError("state is not an object")
    if state.get("version") != 1:
        raise ValueError("unsupported aster state version")
    if not isinstance(state.get("tranches"), dict):
        raise ValueError("state.tranches missing")
    return state


def notional_cap(account_value):
    """Maximum short notional: 10% of account value, times 2x leverage."""
    return float(account_value) * MARGIN_FRACTION * LEVERAGE


def tranche_notional(account_value):
    return notional_cap(account_value) / N_TRANCHES


def round_px(px, sz_decimals, max_decimals=6):
    """Hyperliquid perp price grid.

    Up to 5 significant figures, and no more than (6 - szDecimals) decimal
    places. Integer prices are allowed. Matches Exchange._slippage_price.
    The result is re-emitted from a decimal string so the SDK's float_to_wire
    (8 decimal places, no extra rounding) accepts it.
    """
    px = float(px)
    if px <= 0:
        raise ValueError(f"invalid price {px}")
    decimals = max_decimals - int(sz_decimals)
    if decimals < 0:
        raise ValueError(f"szDecimals {sz_decimals} leaves no price precision")
    rounded = round(float(f"{px:.5g}"), decimals)
    text = f"{rounded:.{decimals}f}"
    out = float(text)
    wire = f"{out:.8f}"
    if abs(float(wire) - out) >= 1e-12:
        raise ValueError(f"price {out} is not wire-safe")
    return out


def floor_sz(sz, sz_decimals):
    """Lot size. Never rounds up, so a floored order cannot cross the cap."""
    sz = float(sz)
    if sz <= 0:
        return 0.0
    quant = Decimal(10) ** -int(sz_decimals)
    floored = Decimal(str(sz)).quantize(quant, rounding=ROUND_DOWN)
    return float(floored)


def size_increment(sz_decimals):
    return float(Decimal(10) ** -int(sz_decimals))


def make_cloid(purpose, salt=""):
    raw = f"aster-unlock-v1:{purpose}:{salt}".encode()
    return "0x" + hashlib.sha256(raw).hexdigest()[:32]


def live_trading_enabled(aster_live, event_name, dispatch_dry_run):
    """Real orders require the repo variable to be exactly the string true.

    Scheduled runs then trade. A manual run also requires dry_run=false.
    Any other event, including a local shell and a push of this branch, stays
    a dry run even if ASTER_LIVE is set.
    """
    if (aster_live or "").strip() != "true":
        return False
    event = (event_name or "").strip()
    if event == "workflow_dispatch":
        return (dispatch_dry_run or "").strip().lower() == "false"
    return event == "schedule"


def live_block_reason(aster_live, event_name, dispatch_dry_run):
    if live_trading_enabled(aster_live, event_name, dispatch_dry_run):
        return None
    if (aster_live or "").strip() != "true":
        return "ASTER_LIVE is not exactly 'true', so this run cannot send orders."
    event = (event_name or "").strip()
    if event == "workflow_dispatch":
        return "workflow_dispatch dry_run is not false, so this run cannot send orders."
    return "only a schedule, or a dispatch with dry_run=false, can send orders when ASTER_LIVE is true."


def resolve_action(explicit, schedule, now):
    action = (explicit or "").strip()
    if action:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        return action
    cron = (schedule or "").strip()
    if not cron:
        return "status"
    if cron in ENTRY_CRONS:
        return "enter_tranche"
    if cron in EXIT_CRONS:
        return "close_all"
    if cron == STOP_CRON:
        return "check_stop"
    # GitHub has historically delayed this repo's crons by hours, and the
    # schedule string is only a hint. A delayed tranche-day run should still
    # enter; a delayed Nov 5 run should still flatten.
    day = now.astimezone(timezone.utc).date()
    hour = now.astimezone(timezone.utc).hour
    if day in TRANCHE_DATES and 12 <= hour < 20 and now < TIME_EXIT:
        return "enter_tranche"
    if day == TIME_EXIT.date() and hour >= 12 and now <= MANAGE_UNTIL:
        return "close_all"
    return "check_stop"


def schedule_in_window(action, now, event_name):
    """Scheduled runs outside the campaign do not touch the account.

    Manual dispatches are always allowed so a leftover position can be
    inspected or closed after the crons have stopped being useful.
    """
    if (event_name or "").strip() != "schedule":
        return True
    now = now.astimezone(timezone.utc)
    if action == "enter_tranche":
        return TRANCHES[0][1] <= now < TIME_EXIT
    if action in ("check_stop", "close_all"):
        return CAMPAIGN_START <= now <= MANAGE_UNTIL
    return True


def maintenance_rate(max_leverage):
    """Half of initial margin at max leverage, per Hyperliquid's liquidation docs."""
    if max_leverage <= 0:
        raise ValueError("max leverage must be positive")
    return 1.0 / (2.0 * float(max_leverage))


def isolated_short_liq_price(entry, leverage=LEVERAGE, max_leverage=5):
    """Isolated short liquidation price at the moment of entry.

    liq_price = price - side * margin_available / position_size / (1 - l * side)
    with side = -1, isolated margin = notional / leverage, and l = 1/(2*maxLeverage).
    Maintenance is evaluated at the entry price; the (1 - l*side) term accounts
    for maintenance changing as price moves. This is an estimate: funding and
    other positions move the real liquidation price, which the exchange reports
    once the position exists.
    """
    entry = float(entry)
    l = maintenance_rate(max_leverage)
    side = -1.0
    isolated = entry / float(leverage)
    margin_available = isolated - entry * l
    if margin_available <= 0:
        return entry
    return entry - side * margin_available / (1.0 - l * side)


def split_tp_sizes(position_sz, sz_decimals):
    """Half the position at the first target; the remainder at the second.

    Odd lots give the extra coin to the runner. A position too small to split
    is closed entirely at the nearer target (0.665).
    """
    sz = floor_sz(position_sz, sz_decimals)
    if sz <= 0:
        return 0.0, 0.0
    half = floor_sz(sz / 2.0, sz_decimals)
    rest = floor_sz(sz - half, sz_decimals)
    if half <= 0:
        return rest, 0.0
    return half, rest


def _limit_buy(trigger_px, sz_decimals, slippage=TRIGGER_SLIPPAGE):
    """Worst price on a buy that closes a short. Must sit above the trigger."""
    trigger = round_px(trigger_px, sz_decimals)
    limit = round_px(trigger_px * (1.0 + slippage), sz_decimals)
    if limit <= trigger:
        raise ValueError(f"buy limit {limit} is not above trigger {trigger}")
    return trigger, limit


def trigger_close_order(sz, trigger_px, tpsl, purpose, sz_decimals):
    trigger, limit = _limit_buy(trigger_px, sz_decimals)
    return {
        "coin": SYMBOL,
        "is_buy": True,
        "sz": floor_sz(sz, sz_decimals),
        "limit_px": limit,
        "order_type": {"trigger": {"isMarket": True, "triggerPx": trigger, "tpsl": tpsl}},
        "reduce_only": True,
        "cloid": make_cloid(purpose),
        "purpose": purpose,
    }


def ioc_close_order(sz, mid, is_buy, purpose, sz_decimals, salt=""):
    mid = float(mid)
    limit = round_px(mid * (1.0 + EXIT_SLIPPAGE) if is_buy else mid * (1.0 - EXIT_SLIPPAGE), sz_decimals)
    return {
        "coin": SYMBOL,
        "is_buy": bool(is_buy),
        "sz": floor_sz(sz, sz_decimals),
        "limit_px": limit,
        "order_type": {"limit": {"tif": "Ioc"}},
        "reduce_only": True,
        "cloid": make_cloid(purpose, salt),
        "purpose": purpose,
    }


def build_protection(position_sz, mark, sz_decimals):
    """Reduce-only exits for an existing short.

    Triggers are fixed size. If mark has already traded through a trigger,
    that slice is an immediate reduce-only IOC instead of a trigger the
    exchange would reject as already executable.
    """
    sz = floor_sz(position_sz, sz_decimals)
    triggers = []
    immediate = []
    if sz <= 0:
        return {"triggers": triggers, "immediate": immediate, "grouping": "na"}
    if float(mark) >= SL_BACKSTOP_PX:
        immediate.append(ioc_close_order(sz, mark, True, "backstop_immediate", sz_decimals, salt="sl"))
        return {"triggers": triggers, "immediate": immediate, "grouping": "na"}

    half, rest = split_tp_sizes(sz, sz_decimals)
    if half > 0 and float(mark) <= TP1_PX:
        immediate.append(ioc_close_order(half, mark, True, "tp1_immediate", sz_decimals, salt="tp1"))
    elif half > 0:
        triggers.append(trigger_close_order(half, TP1_PX, "tp", "tp1", sz_decimals))
    if rest > 0 and float(mark) <= TP2_PX:
        immediate.append(ioc_close_order(rest, mark, True, "tp2_immediate", sz_decimals, salt="tp2"))
    elif rest > 0:
        triggers.append(trigger_close_order(rest, TP2_PX, "tp", "tp2", sz_decimals))
    covered = sum(order["sz"] for order in immediate)
    sl_sz = floor_sz(sz - covered, sz_decimals)
    if sl_sz > 0:
        triggers.append(trigger_close_order(sl_sz, SL_BACKSTOP_PX, "sl", "sl", sz_decimals))
    return {"triggers": triggers, "immediate": immediate, "grouping": "na"}


def entry_order(sz, mid, sz_decimals, tranche_id, filled_sz):
    limit = round_px(float(mid) * (1.0 - ENTRY_SLIPPAGE), sz_decimals)
    return {
        "coin": SYMBOL,
        "is_buy": False,
        "sz": floor_sz(sz, sz_decimals),
        "limit_px": limit,
        "order_type": {"limit": {"tif": "Ioc"}},
        "reduce_only": False,
        "cloid": make_cloid("entry", f"{tranche_id}:{floor_sz(filled_sz, sz_decimals)}"),
        "purpose": "entry_short",
    }


def leverage_action():
    # isCross must be explicit: the SDK defaults update_leverage to cross.
    return {"type": "updateLeverage", "coin": SYMBOL, "leverage": LEVERAGE, "isCross": False}


def _short_sz(position_sz):
    return max(0.0, -float(position_sz))


def _filled_total(state):
    total = 0.0
    for rec in state.get("tranches", {}).values():
        total += float(rec.get("filled_sz") or 0)
    return total


def _size_against_limits(account_value, free_margin, mark, live_short_sz, want_sz, sz_decimals):
    """Shrink want_sz so the short stays inside the notional cap and free margin."""
    mark = float(mark)
    cap = notional_cap(account_value)
    # live_short_sz is already an absolute coin size. Do not flip the sign again.
    live_ntl = max(0.0, float(live_short_sz)) * mark
    room = max(0.0, cap - live_ntl)
    margin_ntl = max(0.0, float(free_margin) * MARGIN_BUFFER * LEVERAGE)
    allowed_ntl = min(room, margin_ntl)
    if want_sz is None:
        desired_ntl = min(tranche_notional(account_value), allowed_ntl)
    else:
        desired_ntl = min(float(want_sz) * mark, allowed_ntl)
    sz = floor_sz(desired_ntl / mark, sz_decimals) if mark > 0 else 0.0
    step = size_increment(sz_decimals)
    while sz > 0 and (live_ntl + sz * mark) > cap + 1e-6:
        sz = floor_sz(sz - step, sz_decimals)
    notional = sz * mark
    binding = None
    uncapped = tranche_notional(account_value) if want_sz is None else float(want_sz) * mark
    if room + 1e-9 < uncapped and (want_sz is None or room + 1e-9 < float(want_sz) * mark):
        binding = "notional_cap"
    if margin_ntl + 1e-9 < min(uncapped, room):
        binding = "free_margin"
    below_min = sz <= 0 or notional + 1e-9 < MIN_ORDER_NOTIONAL_USD
    return {
        "sz": sz,
        "notional_usd": notional,
        "margin_usd": notional / LEVERAGE,
        "notional_cap_usd": cap,
        "tranche_target_notional_usd": tranche_notional(account_value),
        "live_notional_usd": live_ntl,
        "room_notional_usd": room,
        "binding": binding,
        "below_min": below_min,
    }


def _next_tranche(now, state):
    now = now.astimezone(timezone.utc)
    for tid, due in TRANCHES:
        rec = state.get("tranches", {}).get(tid) or {}
        if rec.get("status") == "filled":
            continue
        if now < due:
            return tid, "not_due", due
        return tid, "due", due
    return None, "all_filled", None


def _stand_down(reason, **extra):
    out = {
        "kind": "stand_down",
        "send": False,
        "reason": reason,
        "tranche_id": None,
        "eligible_now": False,
        "leverage": None,
        "entry_order": None,
        "protection": {"triggers": [], "immediate": [], "grouping": "na"},
        "warnings": [],
        "complete_tranche": False,
        "sizing": None,
    }
    out.update(extra)
    return out


def decide_entry(*, now, state, account_value, free_margin, mark, mid, position_sz,
                 sz_decimals, max_leverage, stop_already_triggered=False):
    """Decide whether a tranche may be sent right now, and the exact orders.

    `send` is true only when an order is eligible. Callers still have to pass
    the live-trading gate before transmitting. A tranche that is not due yet
    returns the same payload with send=false so a dry run can show it.
    """
    validate_state(state)
    now = now.astimezone(timezone.utc)
    warnings = []
    if int(max_leverage) < LEVERAGE:
        return _stand_down("max_leverage_below_plan", warnings=[
            f"ASTER max leverage is {max_leverage}x; the plan needs {LEVERAGE}x."
        ])
    if float(account_value) <= 0 or float(mark) <= 0 or float(mid) <= 0:
        return _stand_down("missing_account_or_price")

    tol = size_increment(sz_decimals)
    short_sz = _short_sz(position_sz)
    filled = _filled_total(state)
    if float(position_sz) > tol:
        return _stand_down("long_position_open", warnings=[
            "An ASTER long is open. This campaign only adds to its own short and will not touch it."
        ])
    if state.get("exit"):
        if short_sz > tol:
            return _stand_down("position_open_after_exit")
        return {
            "kind": "skip", "send": False, "reason": "campaign_already_exited",
            "tranche_id": None, "eligible_now": False, "leverage": None,
            "entry_order": None, "protection": build_protection(0, mark, sz_decimals),
            "warnings": warnings, "complete_tranche": False, "sizing": None,
        }
    if filled <= tol and short_sz > tol:
        return _stand_down("unrecognized_short", warnings=[
            "An ASTER short is open that this campaign did not record. Standing down so it is not doubled."
        ])
    if short_sz > filled + tol:
        return _stand_down("position_larger_than_recorded", warnings=[
            f"Short size {short_sz} is above recorded fills {filled}. Standing down."
        ])

    liq = isolated_short_liq_price(mark, LEVERAGE, max_leverage)
    if liq <= SL_BACKSTOP_PX * 1.03:
        warnings.append(
            f"Estimated isolated liquidation {liq:.5g} is within 3% of the {SL_BACKSTOP_PX} backstop at this mark. "
            "The 4h close stop is still the primary exit, but a new tranche here has little room."
        )

    if now >= TIME_EXIT:
        return {
            "kind": "skip", "send": False, "reason": "past_time_exit",
            "tranche_id": None, "eligible_now": False, "leverage": None,
            "entry_order": None, "protection": build_protection(short_sz, mark, sz_decimals),
            "warnings": warnings, "complete_tranche": False, "sizing": None,
        }
    if stop_already_triggered:
        return {
            "kind": "skip", "send": False, "reason": "stop_already_triggered",
            "tranche_id": None, "eligible_now": False, "leverage": None,
            "entry_order": None, "protection": build_protection(short_sz, mark, sz_decimals),
            "warnings": warnings + ["A closed 4h candle is already above 0.785. No further entries."],
            "complete_tranche": False, "sizing": None,
        }
    if float(mark) >= SL_BACKSTOP_PX:
        return {
            "kind": "skip", "send": False, "reason": "mark_at_or_through_backstop",
            "tranche_id": None, "eligible_now": False, "leverage": None,
            "entry_order": None, "protection": build_protection(short_sz, mark, sz_decimals),
            "warnings": warnings, "complete_tranche": False, "sizing": None,
        }
    if float(mark) <= TP2_PX:
        return {
            "kind": "skip", "send": False, "reason": "mark_already_through_take_profits",
            "tranche_id": None, "eligible_now": False, "leverage": None,
            "entry_order": None, "protection": build_protection(short_sz, mark, sz_decimals),
            "warnings": warnings, "complete_tranche": False, "sizing": None,
        }

    tid, due_state, due = _next_tranche(now, state)
    if tid is None:
        return {
            "kind": "skip", "send": False, "reason": "all_tranches_filled",
            "tranche_id": None, "eligible_now": False, "leverage": None,
            "entry_order": None, "protection": build_protection(short_sz, mark, sz_decimals),
            "warnings": warnings, "complete_tranche": False, "sizing": None,
        }

    rec = state.get("tranches", {}).get(tid) or {}
    prior_filled = float(rec.get("filled_sz") or 0)
    target = rec.get("target_sz")
    if target is not None and rec.get("status") == "partial":
        remain = float(target) - prior_filled
        remain_ntl = remain * float(mark)
        if remain <= tol or remain_ntl < MIN_ORDER_NOTIONAL_USD:
            return {
                "kind": "skip", "send": False, "reason": "remainder_below_min_notional",
                "tranche_id": tid, "due_at": due.isoformat(), "eligible_now": False,
                "leverage": None, "entry_order": None,
                "protection": build_protection(short_sz, mark, sz_decimals),
                "warnings": warnings,
                "complete_tranche": True,
                "sizing": {"target_sz": float(target), "filled_sz": prior_filled, "remainder_sz": remain},
            }
        sized = _size_against_limits(
            account_value, free_margin, mark, short_sz, remain, sz_decimals
        )
        frozen_target = float(target)
    else:
        raw_sz = floor_sz(tranche_notional(account_value) / float(mark), sz_decimals)
        sized = _size_against_limits(
            account_value, free_margin, mark, short_sz, None, sz_decimals
        )
        frozen_target = raw_sz

    if sized["binding"]:
        warnings.append(f"Tranche size was reduced by {sized['binding']}.")
    base = {
        "tranche_id": tid,
        "due_at": due.isoformat(),
        "warnings": warnings,
        "complete_tranche": False,
        "sizing": sized,
        "target_sz": frozen_target,
        "prior_filled_sz": prior_filled,
    }
    if sized["below_min"] or sized["sz"] <= 0:
        reason = "notional_cap" if sized["room_notional_usd"] < MIN_ORDER_NOTIONAL_USD else "below_min_notional"
        if sized["binding"] == "free_margin" and sized["sz"] <= 0:
            reason = "no_free_margin"
        base.update({
            "kind": "skip", "send": False, "reason": reason, "eligible_now": False,
            "leverage": None, "entry_order": None,
            "protection": build_protection(short_sz, mark, sz_decimals),
        })
        return base

    order = entry_order(sized["sz"], mid, sz_decimals, tid, prior_filled)
    expected_short = short_sz + sized["sz"]
    protection = build_protection(expected_short, mark, sz_decimals)
    eligible = due_state == "due"
    base.update({
        "kind": "enter" if eligible else "preview",
        "send": eligible,
        "reason": "due" if eligible else "not_due",
        "eligible_now": eligible,
        "leverage": leverage_action(),
        "entry_order": order,
        "protection": protection,
        "protective_on_filled_position_sz": expected_short,
    })
    return base


def latest_closed_4h(candles, now):
    """Most recent 4h candle whose close time is already in the past.

    The snapshot includes the forming candle. A wick through 0.785 does not
    stop the trade; only that candle's close does, and only once it has closed.
    """
    now_ms = int(now.timestamp() * 1000) if isinstance(now, datetime) else int(now)
    closed = [c for c in candles or [] if int(c.get("T", 0)) <= now_ms]
    if not closed:
        return {"found": False, "stop": False, "reason": "no_closed_candle"}
    last = max(closed, key=lambda c: int(c["T"]))
    close = float(last["c"])
    return {
        "found": True,
        "stop": close > CLOSE_STOP_PX,
        "close": close,
        "high": float(last["h"]),
        "low": float(last["l"]),
        "open": float(last["o"]),
        "open_time_ms": int(last["t"]),
        "close_time_ms": int(last["T"]),
        "reason": "4h_close_above_stop" if close > CLOSE_STOP_PX else "4h_close_not_above_stop",
    }


def campaign_short(state, position_sz, sz_decimals):
    """Size of the short this campaign may manage.

    Returns 0 when flat, the short size when it matches recorded fills, and
    None when the position is a long, reappeared after the exit, or is larger
    than anything this campaign recorded. Callers must not send orders on None.
    """
    validate_state(state)
    tol = size_increment(sz_decimals)
    if float(position_sz) > tol:
        return None
    short = _short_sz(position_sz)
    if short <= tol:
        return 0.0
    if state.get("exit"):
        return None
    filled = _filled_total(state)
    if filled <= tol or short > filled + tol:
        return None
    return short


def decide_check_stop(*, now, candles, position_sz, mark, mid, sz_decimals, state):
    validate_state(state)
    now = now.astimezone(timezone.utc)
    candle = latest_closed_4h(candles, now)
    owned = campaign_short(state, position_sz, sz_decimals)
    if owned is None:
        return {
            "kind": "stand_down", "send": False, "reason": "position_not_ours",
            "candle": candle, "orders": [],
            "protection": {"triggers": [], "immediate": [], "grouping": "na"},
            "warnings": ["ASTER position is not one this campaign recorded. Not closing or replacing orders."],
        }
    if state.get("exit") and owned == 0:
        return {"kind": "skip", "send": False, "reason": "already_exited", "candle": candle,
                "orders": [], "protection": build_protection(0, mark, sz_decimals)}
    if owned > 0 and now >= TIME_EXIT:
        return _close_decision(-owned, mid, sz_decimals, "time_exit", candle)
    if owned > 0 and candle.get("stop"):
        return _close_decision(-owned, mid, sz_decimals, "4h_close_above_0.785", candle)
    if owned > 0 and float(mark) >= SL_BACKSTOP_PX:
        return _close_decision(-owned, mid, sz_decimals, "mark_through_backstop", candle)
    if owned == 0:
        return {"kind": "skip", "send": False, "reason": "flat", "candle": candle,
                "orders": [], "protection": build_protection(0, mark, sz_decimals)}
    return {
        "kind": "protect",
        "send": False,
        "reason": "position_open",
        "candle": candle,
        "orders": [],
        "protection": build_protection(owned, mark, sz_decimals),
    }


def decide_close_all(*, position_sz, mid, sz_decimals, reason="manual_close_all"):
    return _close_decision(position_sz, mid, sz_decimals, reason, candle=None)


def _close_decision(position_sz, mid, sz_decimals, reason, candle):
    sz = floor_sz(abs(float(position_sz)), sz_decimals)
    if sz <= 0 or float(mid) <= 0:
        return {"kind": "skip", "send": False, "reason": "already_flat", "candle": candle,
                "orders": [], "protection": {"triggers": [], "immediate": [], "grouping": "na"}}
    is_buy = float(position_sz) < 0
    order = ioc_close_order(sz, mid, is_buy, "close", sz_decimals, salt=reason)
    return {
        "kind": "close",
        "send": True,
        "reason": reason,
        "candle": candle,
        "orders": [order],
        "cancel_triggers_after": True,
        "protection": {"triggers": [], "immediate": [], "grouping": "na"},
    }


def apply_fill(state, tranche_id, target_sz, filled_delta, now, mark):
    """Record a confirmed fill. A second call with delta 0 does not add size."""
    state = deepcopy(state)
    validate_state(state)
    rec = dict(state["tranches"].get(tranche_id) or {})
    target = float(rec.get("target_sz") or target_sz)
    filled = float(rec.get("filled_sz") or 0) + float(filled_delta)
    status = "filled" if filled + 1e-9 >= target else "partial"
    state["tranches"][tranche_id] = {
        "status": status,
        "target_sz": target,
        "filled_sz": filled,
        "mark_at_entry": rec.get("mark_at_entry", float(mark)),
        "notional_usd": filled * float(mark),
        "filled_at": now.astimezone(timezone.utc).isoformat(),
    }
    return state


def mark_tranche_filled(state, tranche_id, now, reason):
    state = deepcopy(state)
    validate_state(state)
    rec = dict(state["tranches"].get(tranche_id) or {})
    filled = float(rec.get("filled_sz") or 0)
    target = float(rec.get("target_sz") or filled)
    state["tranches"][tranche_id] = {
        "status": "filled",
        "target_sz": target,
        "filled_sz": filled,
        "mark_at_entry": rec.get("mark_at_entry"),
        "notional_usd": rec.get("notional_usd"),
        "filled_at": rec.get("filled_at") or now.astimezone(timezone.utc).isoformat(),
        "completed_reason": reason,
    }
    return state


def mark_exit(state, reason, now, price):
    state = deepcopy(state)
    validate_state(state)
    state["exit"] = {
        "reason": reason,
        "at": now.astimezone(timezone.utc).isoformat(),
        "price": None if price is None else float(price),
    }
    return state


def protection_matches(open_orders, protection, sz_decimals):
    """True when each desired trigger is already resting at the same size and price."""
    triggers = []
    for order in open_orders or []:
        if order.get("coin") != SYMBOL:
            continue
        if not (order.get("isTrigger") or order.get("triggerPx") not in (None, "", "0")):
            continue
        if order.get("triggerPx") in (None, "", "0"):
            continue
        triggers.append(order)
    desired = protection.get("triggers") or []
    if protection.get("immediate"):
        return False
    used = set()
    tol = size_increment(sz_decimals) / 2 if sz_decimals else 0.5
    for want in desired:
        want_px = want["order_type"]["trigger"]["triggerPx"]
        found = False
        for i, order in enumerate(triggers):
            if i in used:
                continue
            try:
                px = float(order.get("triggerPx"))
                sz = float(order.get("sz"))
            except (TypeError, ValueError):
                continue
            if abs(px - want_px) > 1e-8 or abs(sz - want["sz"]) > tol:
                continue
            side = order.get("side")
            if side not in (None, "B"):
                continue
            if order.get("reduceOnly") is False:
                continue
            used.add(i)
            found = True
            break
        if not found:
            return False
    return True


def describe_submission(decision):
    """JSON-ready description of what a live eligible run would transmit."""
    entry = decision.get("entry_order")
    protection = decision.get("protection") or {}
    return {
        "eligible_now": bool(decision.get("eligible_now")),
        "send": bool(decision.get("send")),
        "reason": decision.get("reason"),
        "leverage": decision.get("leverage"),
        "entry": None if not entry else {
            "grouping": "na",
            "order": entry,
        },
        "after_fill": {
            "grouping": protection.get("grouping", "na"),
            "triggers": protection.get("triggers") or [],
            "immediate_reduce_only": protection.get("immediate") or [],
            "assumes_position_sz": decision.get("protective_on_filled_position_sz"),
        },
    }
