#!/usr/bin/env python3
"""
ASTER unlock short on Hyperliquid mainnet.

Dry run unless the repository variable ASTER_LIVE is exactly "true". A
workflow_dispatch also needs its dry_run input set to false. Scheduled
crons are dry runs until that variable is set. This file is the only one
that talks to the exchange; sizing and idempotency live in aster_unlock_plan.py.

Read-only calls (account, mark, funding, candles, open orders) run either
way so a dry run shows the real account. Mutating calls go through Sender,
which recomputes the live gate and refuses to construct an Exchange client
unless the gate is open.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from aster_unlock_plan import (
    EXIT_SLIPPAGE,
    LEVERAGE,
    SL_BACKSTOP_PX,
    SYMBOL,
    TIME_EXIT,
    TP1_PX,
    TP2_PX,
    apply_fill,
    build_protection,
    decide_check_stop,
    decide_close_all,
    decide_entry,
    describe_submission,
    empty_state,
    latest_closed_4h,
    live_block_reason,
    live_trading_enabled,
    make_cloid,
    mark_exit,
    mark_tranche_filled,
    protection_matches,
    resolve_action,
    schedule_in_window,
    validate_state,
)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "aster_unlock_state.json")

PRIVATE_KEY = os.environ.get("HYPERLIQUID_API_PRIVATE_KEY", "")
ACCOUNT_ADDRESS = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "")


def _now():
    return datetime.now(timezone.utc)


def _env(name, default=""):
    return os.environ.get(name, default)


def notify(msg):
    try:
        from divergence_monitor import tg
        tg(msg)
    except Exception as exc:
        print(f"  [telegram unavailable] {type(exc).__name__}")
        print(msg)


def _html(text):
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def load_state():
    if not os.path.exists(STATE_FILE):
        return empty_state()
    with open(STATE_FILE) as handle:
        state = json.load(handle)
    return validate_state(state)


def save_state(state):
    validate_state(state)
    state = dict(state)
    state["updated_at"] = _now().isoformat()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, STATE_FILE)


def _wallet(key):
    from eth_account import Account
    try:
        return Account.from_key(key)
    except Exception as exc:
        raise SystemExit(f"invalid HYPERLIQUID_API_PRIVATE_KEY ({type(exc).__name__})") from None


class Sender:
    """Mutating exchange calls. Impossible to use while the live gate is shut."""

    def __init__(self, live):
        self.live = bool(live)
        self.sent = 0
        self.exchange = None
        if not self.live:
            return
        if not PRIVATE_KEY or not ACCOUNT_ADDRESS:
            raise SystemExit("ASTER_LIVE is armed but Hyperliquid credentials are missing.")
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        self.exchange = Exchange(
            _wallet(PRIVATE_KEY),
            base_url=constants.MAINNET_API_URL,
            account_address=ACCOUNT_ADDRESS,
        )

    def call(self, label, fn):
        armed = live_trading_enabled(
            _env("ASTER_LIVE"),
            _env("ASTER_EVENT_NAME") or _env("GITHUB_EVENT_NAME"),
            _env("ASTER_DISPATCH_DRY_RUN"),
        )
        if not self.live or not armed or self.exchange is None:
            raise RuntimeError(f"refusing to send {label}: live trading is not armed")
        response = fn()
        self.sent += 1
        print(f"SENT {label} {_dump(response)[:2000]}")
        return response


def _dump(payload):
    return json.dumps(payload, default=str, sort_keys=True)


def _info():
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    return Info(constants.MAINNET_API_URL, skip_ws=True)


def _market(info):
    meta, ctxs = info.meta_and_asset_ctxs()
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("name") == SYMBOL:
            return asset, ctx
    raise SystemExit(f"{SYMBOL} is not listed in the Hyperliquid perp universe.")


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _spot_balances(info):
    spot = info.spot_user_state(ACCOUNT_ADDRESS)
    balances = []
    usdc_total = 0.0
    usdc_hold = 0.0
    for row in spot.get("balances") or []:
        total = _f(row.get("total"))
        hold = _f(row.get("hold"))
        if total == 0 and hold == 0:
            continue
        balances.append({"coin": row.get("coin"), "total": total, "hold": hold})
        if row.get("coin") == "USDC":
            usdc_total = total
            usdc_hold = hold
    return balances, usdc_total, usdc_hold


def _abstraction(info):
    try:
        mode = info.query_user_abstraction_state(ACCOUNT_ADDRESS)
    except Exception as exc:
        return f"unavailable:{type(exc).__name__}"
    if isinstance(mode, str):
        return mode.strip().strip('"')
    return str(mode)


def _portfolio_latest(info):
    """Latest point on Hyperliquid's portfolio curve. Not used for sizing."""
    try:
        data = info.portfolio(ACCOUNT_ADDRESS)
    except Exception as exc:
        return {"error": type(exc).__name__}
    latest = None
    if isinstance(data, list):
        for item in data:
            if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict)):
                continue
            history = item[1].get("accountValueHistory") or []
            if not history:
                continue
            ts, value = history[-1]
            if latest is None or int(ts) >= latest["ts"]:
                latest = {"period": item[0], "ts": int(ts), "account_value": value}
    return latest or {"latest": None}


def _account(info):
    us = info.user_state(ACCOUNT_ADDRESS)
    margin = us.get("marginSummary") or {}
    cross = us.get("crossMarginSummary") or {}
    perp_margin = _f(margin.get("accountValue"))
    perp_cross = _f(cross.get("accountValue"))
    perp_value = max(perp_margin, perp_cross)
    balances, usdc_total, usdc_hold = _spot_balances(info)
    mode = _abstraction(info)
    # Unified and portfolio-margin accounts keep the tradable balance in the
    # spot clearinghouse. Their perp marginSummary is not meaningful. Standard
    # accounts report equity on the perp clearinghouse instead.
    unified = mode in ("unifiedAccount", "portfolioMargin")
    if unified:
        account_value = usdc_total
        source = "spot_usdc_unified"
    elif perp_value > 0:
        account_value = perp_value
        source = "perp_margin_summary"
    else:
        account_value = usdc_total
        source = "spot_usdc"
    margin_used = max(_f(margin.get("totalMarginUsed")), _f(cross.get("totalMarginUsed")))
    withdrawable = _f(us.get("withdrawable"))
    classic = account_value - margin_used
    if withdrawable > 0 and classic > 0:
        free = min(withdrawable, max(classic, 0.0))
    elif withdrawable > 0:
        free = withdrawable
    else:
        free = max(0.0, classic)
    # Spot USDC on hold is not free even when the perp withdrawable field is empty.
    if unified:
        free = max(0.0, usdc_total - usdc_hold)
    return {
        "user_state": us,
        "account_value": account_value,
        "margin_used": margin_used,
        "withdrawable": withdrawable,
        "free_margin": free,
        "source": source,
        "abstraction": mode,
        "perp_account_value": perp_value,
        "spot_balances": balances,
        "portfolio_latest": _portfolio_latest(info),
    }


def _position(user_state):
    for wrapper in user_state.get("assetPositions") or []:
        position = wrapper.get("position") or {}
        if position.get("coin") == SYMBOL and _f(position.get("szi")) != 0:
            return position
    return None


def _position_view(position):
    if not position:
        return {"sz": 0.0, "entry_px": None, "liquidation_px": None,
                "unrealized_pnl": None, "margin_used": None, "leverage": None}
    leverage = position.get("leverage") or {}
    return {
        "sz": _f(position.get("szi")),
        "entry_px": _f(position.get("entryPx"), None) if position.get("entryPx") is not None else None,
        "liquidation_px": _f(position.get("liquidationPx")) if position.get("liquidationPx") not in (None, "") else None,
        "unrealized_pnl": _f(position.get("unrealizedPnl")) if position.get("unrealizedPnl") not in (None, "") else None,
        "margin_used": _f(position.get("marginUsed")) if position.get("marginUsed") not in (None, "") else None,
        "leverage": leverage,
    }


def _agent_report(info):
    if not PRIVATE_KEY or not ACCOUNT_ADDRESS:
        return {"checked": False, "reason": "credentials missing"}
    agent = _wallet(PRIVATE_KEY).address
    report = {
        "checked": False,
        "agent": agent,
        "account_is_agent": agent.lower() == ACCOUNT_ADDRESS.lower(),
    }
    try:
        payload = info.extra_agents(ACCOUNT_ADDRESS)
    except Exception as exc:
        report["error"] = type(exc).__name__
        return report
    agents = payload if isinstance(payload, list) else []
    match = next((item for item in agents if str(item.get("address", "")).lower() == agent.lower()), None)
    now_ms = int(_now().timestamp() * 1000)
    valid_until = None if not match else match.get("validUntil")
    valid_ms = None
    if valid_until is not None:
        valid_ms = int(valid_until)
        if valid_ms < 10**11:
            valid_ms *= 1000
    report.update({
        "checked": True,
        "approved": match is not None and (valid_ms is None or valid_ms > now_ms),
        "valid_until_ms": valid_ms,
        "name": None if not match else match.get("name"),
        "listed_agents": len(agents),
    })
    return report


def _candles(info, now):
    end_ms = int(now.timestamp() * 1000)
    start_ms = end_ms - 10 * 24 * 3600 * 1000
    return info.candles_snapshot(SYMBOL, "4h", start_ms, end_ms)


def _mid(info, ctx):
    try:
        mids = info.all_mids()
        if SYMBOL in mids:
            return float(mids[SYMBOL])
    except Exception as exc:
        print(f"  mid fallback: {type(exc).__name__}")
    for key in ("midPx", "markPx"):
        if ctx.get(key):
            return float(ctx[key])
    raise SystemExit("no ASTER mid or mark price")


def _print_orders(title, payload):
    print(title)
    print(json.dumps(payload, indent=2, default=str, sort_keys=True))


def _maybe_notify(event_name, interesting, text):
    if event_name == "workflow_dispatch" or interesting:
        notify(text)


def _accepted(response):
    return isinstance(response, dict) and response.get("status") == "ok"


def _execute_entry(sender, info, decision, state, mark, sz_decimals, now):
    from hyperliquid.utils.types import Cloid

    order = decision["entry_order"]
    before = _position_view(_position(info.user_state(ACCOUNT_ADDRESS)))["sz"]
    # is_cross=False is required. Exchange.update_leverage defaults to cross.
    leverage = sender.call(
        "update_leverage",
        lambda: sender.exchange.update_leverage(LEVERAGE, SYMBOL, is_cross=False),
    )
    if not _accepted(leverage):
        raise SystemExit("update_leverage was rejected; entry aborted and tranche left unfilled")
    response = sender.call(
        "entry",
        lambda: sender.exchange.order(
            order["coin"], order["is_buy"], order["sz"], order["limit_px"],
            order["order_type"], reduce_only=False, cloid=Cloid(order["cloid"]),
        ),
    )
    after_state = info.user_state(ACCOUNT_ADDRESS)
    after = _position_view(_position(after_state))["sz"]
    delta = max(0.0, -after) - max(0.0, -before)
    print(f"entry response status={response.get('status') if isinstance(response, dict) else type(response).__name__} "
          f"short_delta={delta}")
    if delta <= 0:
        raise SystemExit("entry was not filled; tranche left unfilled so a later run can retry")
    state = apply_fill(state, decision["tranche_id"], decision["target_sz"], delta, now, mark)
    save_state(state)
    _refresh_protection(sender, info, abs(after), mark, sz_decimals)
    return state


def _refresh_protection(sender, info, position_sz, mark, sz_decimals):
    from hyperliquid.utils.types import Cloid

    protection = build_protection(position_sz, mark, sz_decimals)
    _cancel_our_triggers(sender, info)
    for order in protection["immediate"]:
        sender.call(
            order["purpose"],
            lambda order=order: sender.exchange.order(
                order["coin"], order["is_buy"], order["sz"], order["limit_px"],
                order["order_type"], reduce_only=True, cloid=Cloid(order["cloid"]),
            ),
        )
    triggers = protection["triggers"]
    if not triggers:
        return protection
    def _place(triggers=triggers):
        requests = []
        for order in triggers:
            requests.append({
                "coin": order["coin"],
                "is_buy": order["is_buy"],
                "sz": order["sz"],
                "limit_px": order["limit_px"],
                "order_type": order["order_type"],
                "reduce_only": True,
                "cloid": Cloid(order["cloid"]),
            })
        return sender.exchange.bulk_orders(requests, grouping="na")
    sender.call("protective_triggers", _place)
    return protection


def _flatten_orders(orders):
    flat = []
    for order in orders or []:
        flat.append(order)
        flat.extend(_flatten_orders(order.get("children")))
    return flat


def _cancel_our_triggers(sender, info):
    from hyperliquid.utils.types import Cloid

    cloids = [Cloid(make_cloid(purpose)) for purpose in ("tp1", "tp2", "sl")]
    sender.call(
        "cancel_cloids",
        lambda: sender.exchange.bulk_cancel_by_cloid(
            [{"coin": SYMBOL, "cloid": cloid} for cloid in cloids]
        ),
    )
    try:
        open_orders = _flatten_orders(info.frontend_open_orders(ACCOUNT_ADDRESS))
    except Exception as exc:
        print(f"  could not list open orders after cancel: {type(exc).__name__}")
        return
    targets = {round(px, 5) for px in (TP1_PX, TP2_PX, SL_BACKSTOP_PX)}
    cancels = []
    for order in open_orders or []:
        if order.get("coin") != SYMBOL or not order.get("isTrigger"):
            continue
        try:
            px = round(float(order.get("triggerPx")), 5)
        except (TypeError, ValueError):
            continue
        if px in targets and order.get("oid") is not None:
            cancels.append({"coin": SYMBOL, "oid": order["oid"]})
    if cancels:
        sender.call("cancel_matching_triggers", lambda: sender.exchange.bulk_cancel(cancels))


def _execute_close(sender, info, decision, state, now, mark):
    from hyperliquid.utils.types import Cloid

    order = decision["orders"][0]
    sender.call(
        "close",
        lambda: sender.exchange.market_close(
            SYMBOL, slippage=EXIT_SLIPPAGE, cloid=Cloid(order["cloid"]),
        ),
    )
    after = _position_view(_position(info.user_state(ACCOUNT_ADDRESS)))
    if abs(after["sz"]) > 0:
        raise SystemExit(f"close did not flatten ASTER (szi={after['sz']}); will retry next run")
    try:
        _cancel_our_triggers(sender, info)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"  trigger cancel after flat close failed: {type(exc).__name__}")
    state = mark_exit(state, decision["reason"], now, mark)
    save_state(state)
    return state


def _context(info, now):
    asset, ctx = _market(info)
    mark = float(ctx["markPx"])
    mid = _mid(info, ctx)
    funding = _f(ctx.get("funding"))
    account = None
    position = {"sz": 0.0}
    orders = []
    if ACCOUNT_ADDRESS:
        account = _account(info)
        position = _position_view(_position(account["user_state"]))
        try:
            orders = _flatten_orders(info.frontend_open_orders(ACCOUNT_ADDRESS))
        except Exception as exc:
            print(f"  open orders unavailable: {type(exc).__name__}")
    candles = _candles(info, now)
    return {
        "asset": asset,
        "ctx": ctx,
        "mark": mark,
        "mid": mid,
        "oracle": _f(ctx.get("oraclePx"), None),
        "funding_hourly": funding,
        "funding_annualized": funding * 24 * 365,
        "open_interest": _f(ctx.get("openInterest"), None),
        "sz_decimals": int(asset["szDecimals"]),
        "max_leverage": int(asset["maxLeverage"]),
        "account": account,
        "position": position,
        "orders": orders,
        "candles": candles,
        "candle": latest_closed_4h(candles, now),
    }


def _header(action, live, reason, ctx, agent):
    account = ctx["account"] or {}
    position = ctx["position"]
    funding = ctx["funding_hourly"]
    direction = "longs pay shorts" if funding > 0 else "shorts pay longs" if funding < 0 else "flat"
    print(f"aster_unlock_short {_now().isoformat()} action={action} live={str(live).lower()}")
    print(f"live_gate: {reason or 'armed'}")
    print(f"account_value={account.get('account_value')} source={account.get('source')} "
          f"abstraction={account.get('abstraction')} perp_account_value={account.get('perp_account_value')}")
    print(f"free_margin={account.get('free_margin')} withdrawable={account.get('withdrawable')} "
          f"margin_used={account.get('margin_used')}")
    print(f"spot_balances={account.get('spot_balances')} portfolio_latest={account.get('portfolio_latest')}")
    print(f"ASTER mark={ctx['mark']} mid={ctx['mid']} oracle={ctx['oracle']} "
          f"szDecimals={ctx['sz_decimals']} maxLeverage={ctx['max_leverage']}")
    print(f"funding_hourly={funding} ({direction}) funding_annualized={ctx['funding_annualized']:.6f} "
          f"open_interest={ctx['open_interest']}")
    print(f"position_sz={position.get('sz')} entry_px={position.get('entry_px')} "
          f"liquidation_px={position.get('liquidation_px')} unrealized_pnl={position.get('unrealized_pnl')} "
          f"leverage={position.get('leverage')}")
    candle = ctx["candle"]
    print(f"last_closed_4h close={candle.get('close')} high={candle.get('high')} "
          f"stop={candle.get('stop')} reason={candle.get('reason')}")
    print(f"backstop={SL_BACKSTOP_PX} time_exit={TIME_EXIT.isoformat()}")
    print("agent=" + _dump(agent))
    if agent.get("account_is_agent"):
        print("WARNING: API wallet address equals the account address. This key can withdraw, "
              "not only trade. An agent (API) wallet is safer.")
    if agent.get("checked") and not agent.get("approved"):
        print("WARNING: this API wallet is not an unexpired approved agent of the account. "
              "Live orders will be rejected until it is approved.")


def run():
    now = _now()
    event_name = (_env("ASTER_EVENT_NAME") or _env("GITHUB_EVENT_NAME")).strip()
    action = resolve_action(_env("ASTER_ACTION"), _env("ASTER_SCHEDULE"), now)
    if not schedule_in_window(action, now, event_name):
        print(f"aster_unlock_short {now.isoformat()} action={action} outside campaign window; no orders")
        print("ORDERS_SENT 0")
        return 0

    live = live_trading_enabled(_env("ASTER_LIVE"), event_name, _env("ASTER_DISPATCH_DRY_RUN"))
    reason = live_block_reason(_env("ASTER_LIVE"), event_name, _env("ASTER_DISPATCH_DRY_RUN"))
    sender = Sender(live)
    info = _info()
    ctx = _context(info, now)
    agent = _agent_report(info)
    _header(action, live, reason, ctx, agent)

    state = load_state()
    account = ctx["account"]
    if action != "status" and account is None:
        raise SystemExit("Hyperliquid account address is not set; refusing to plan an order.")

    result = {"action": action, "live": live, "live_gate": reason or "armed"}
    interesting = False
    exit_code = 0

    if action == "status":
        preview = None
        if account is not None:
            preview = decide_entry(
                now=now, state=state, account_value=account["account_value"],
                free_margin=account["free_margin"], mark=ctx["mark"], mid=ctx["mid"],
                position_sz=ctx["position"]["sz"], sz_decimals=ctx["sz_decimals"],
                max_leverage=ctx["max_leverage"],
                stop_already_triggered=bool(ctx["candle"].get("stop")),
            )
            print(f"next_tranche={preview.get('tranche_id')} reason={preview.get('reason')} "
                  f"eligible_now={preview.get('eligible_now')}")
            sizing = preview.get("sizing") or {}
            print(f"tranche_sz={sizing.get('sz', preview.get('sizing'))} "
                  f"tranche_notional_usd={sizing.get('notional_usd')} "
                  f"tranche_margin_usd={sizing.get('margin_usd')} "
                  f"notional_cap_usd={sizing.get('notional_cap_usd')}")
            if preview.get("entry_order"):
                _print_orders("status preview (not sent by status):", describe_submission(preview))
        result["preview"] = preview
        interesting = event_name == "workflow_dispatch"

    elif action == "enter_tranche":
        decision = decide_entry(
            now=now, state=state, account_value=account["account_value"],
            free_margin=account["free_margin"], mark=ctx["mark"], mid=ctx["mid"],
            position_sz=ctx["position"]["sz"], sz_decimals=ctx["sz_decimals"],
            max_leverage=ctx["max_leverage"],
            stop_already_triggered=bool(ctx["candle"].get("stop")),
        )
        result["decision"] = decision
        sizing = decision.get("sizing") or {}
        print(f"decision={decision['kind']} reason={decision['reason']} tranche={decision.get('tranche_id')} "
              f"send={decision['send']} eligible_now={decision.get('eligible_now')}")
        print(f"tranche_sz={sizing.get('sz')} tranche_notional_usd={sizing.get('notional_usd')} "
              f"tranche_margin_usd={sizing.get('margin_usd')} notional_cap_usd={sizing.get('notional_cap_usd')} "
              f"room_notional_usd={sizing.get('room_notional_usd')}")
        if decision.get("entry_order") or decision.get("leverage"):
            _print_orders("orders:", describe_submission(decision))
        for warning in decision.get("warnings") or []:
            print(f"WARNING: {warning}")
        interesting = True
        if decision["kind"] == "stand_down":
            exit_code = 1
        elif decision.get("complete_tranche") and live:
            state = mark_tranche_filled(state, decision["tranche_id"], now, decision["reason"])
            save_state(state)
            print(f"marked {decision['tranche_id']} filled without an order ({decision['reason']})")
        elif decision.get("complete_tranche"):
            print(f"DRY RUN: would mark {decision['tranche_id']} filled without an order "
                  f"({decision['reason']}). State was not changed.")
        elif decision["send"] and live:
            state = _execute_entry(sender, info, decision, state, ctx["mark"], ctx["sz_decimals"], now)
        elif decision["send"]:
            print("DRY RUN: eligible orders were not sent.")
        else:
            if decision.get("entry_order"):
                print("DRY RUN: tranche is not eligible right now. Payload above is the order a due, "
                      "live run would send from this same account snapshot. Nothing was sent.")
            else:
                print(f"DRY RUN: no order would be sent ({decision['reason']}). Nothing was sent.")

    elif action == "check_stop":
        decision = decide_check_stop(
            now=now, candles=ctx["candles"], position_sz=ctx["position"]["sz"],
            mark=ctx["mark"], mid=ctx["mid"], sz_decimals=ctx["sz_decimals"], state=state,
        )
        result["decision"] = {k: v for k, v in decision.items() if k != "candle"}
        result["candle"] = decision.get("candle")
        print(f"decision={decision['kind']} reason={decision['reason']} send={decision['send']}")
        if decision["send"]:
            _print_orders("close order:", decision["orders"])
            interesting = True
            if live:
                state = _execute_close(sender, info, decision, state, now, ctx["mark"])
            else:
                print("DRY RUN: close was not sent.")
        elif decision["kind"] == "protect":
            matches = protection_matches(ctx["orders"], decision["protection"], ctx["sz_decimals"])
            print(f"protection_matches={matches}")
            _print_orders("desired protection:", decision["protection"])
            if not matches:
                interesting = True
                if live:
                    _refresh_protection(
                        sender, info, abs(ctx["position"]["sz"]), ctx["mark"], ctx["sz_decimals"],
                    )
                else:
                    print("DRY RUN: protection was not replaced.")
            else:
                print("protection already matches; left in place.")
        elif decision["kind"] == "stand_down":
            interesting = True
            exit_code = 1
            for warning in decision.get("warnings") or []:
                print(f"WARNING: {warning}")

    elif action == "close_all":
        # Scheduled time-exit only flattens a short this campaign recorded.
        # A manual close_all is an explicit request to flatten whatever ASTER
        # position is open.
        if event_name == "schedule":
            decision = decide_check_stop(
                now=now, candles=ctx["candles"], position_sz=ctx["position"]["sz"],
                mark=ctx["mark"], mid=ctx["mid"], sz_decimals=ctx["sz_decimals"], state=state,
            )
        else:
            decision = decide_close_all(
                position_sz=ctx["position"]["sz"], mid=ctx["mid"], sz_decimals=ctx["sz_decimals"],
            )
        result["decision"] = decision
        print(f"decision={decision['kind']} reason={decision['reason']} send={decision['send']}")
        interesting = True
        if decision["send"]:
            _print_orders("close order:", decision["orders"])
            if live:
                state = _execute_close(sender, info, decision, state, now, ctx["mark"])
            else:
                print("DRY RUN: close was not sent.")
        elif decision["kind"] == "stand_down":
            exit_code = 1
            for warning in decision.get("warnings") or []:
                print(f"WARNING: {warning}")
        elif decision["kind"] == "protect":
            print("position is still open and the time exit has not been reached; no close order.")
        else:
            print("already flat; no close order.")
    else:
        raise SystemExit(f"unhandled action {action}")

    if not live and sender.sent:
        raise SystemExit("safety violation: an order was sent while the live gate was shut")
    print(f"ORDERS_SENT {sender.sent}")
    print("ASTER_RESULT " + _dump({"exit_code": exit_code, **_public_result(result)}))

    prefix = "🧪 [DRY RUN] " if not live else ""
    _maybe_notify(
        event_name,
        interesting,
        f"{prefix}<b>ASTER unlock { _html(action) }</b>\n"
        f"live={str(live).lower()} {_html(reason or 'armed')}\n"
        f"account_value=${(account or {}).get('account_value')}\n"
        f"free_margin=${(account or {}).get('free_margin')}\n"
        f"mark={ctx['mark']} funding_hourly={ctx['funding_hourly']}\n"
        f"position_sz={ctx['position'].get('sz')}\n"
        f"result={_html(result.get('decision', result.get('preview') or {}).get('reason', action))}\n"
        f"orders_sent={sender.sent}",
    )
    return exit_code


def _public_result(result):
    """Drop raw candles and user state from the log line."""
    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()
                    if k not in {"user_state", "candles", "orders"}}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    return scrub(result)


def main():
    try:
        code = run()
    except SystemExit as exc:
        if exc.code not in (0, None):
            notify(f"🛑 <b>ASTER unlock error</b>\n{_html(exc.code)}")
        raise
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}")
        notify(f"🛑 <b>ASTER unlock error</b>\n{_html(type(exc).__name__)}: {_html(exc)}")
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
