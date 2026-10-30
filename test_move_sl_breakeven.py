"""Breakeven stop move: price math, the live-position guard, and the order it sends."""
import json

import divergence_trader as dt

PRIMARY = {
    "episode_id": "2h-bearish-1793289600",
    "direction": "bearish",
    "size": 0.01285,
    "entry_price": 77832.3,
    "entry_ts": 1793318458,
    "entry_equity": 158.357726,
    "tp_price": 75186.0,
    "sl_price": 79388.9,
    "tp_sl_ok": True,
}
YAR_ENTRY = 77616.0
YAR_TP = 74977.1
YAR_SL = 79168.3


def test_default_protection_is_the_strategy_formula():
    # No overrides must be exactly tp_sl_prices(), limit included. Rounding
    # the stop before the 0.5% limit shifts that limit by a tick.
    for direction, entry in (("bearish", 77832.3), ("bearish", YAR_ENTRY), ("bullish", 50000.0)):
        assert dt.protection_prices(direction, entry) == dt.tp_sl_prices(direction, entry)
    tp, sl, _ = dt.protection_prices("bearish", 77832.3)
    assert (tp, sl) == (75186.0, 79388.9)
    tp, sl, _ = dt.protection_prices("bearish", YAR_ENTRY)
    assert (tp, sl) == (YAR_TP, YAR_SL)


def test_breakeven_limit_is_half_percent_through_the_trigger():
    tp, sl, sl_lim = dt.protection_prices("bearish", 77832.3, tp=75186.0, sl=77832.3)
    assert (tp, sl) == (75186.0, 77832.3)
    assert sl_lim == round(77832.3 * 1.005, 1)
    tp, sl, sl_lim = dt.protection_prices("bearish", YAR_ENTRY, tp=YAR_TP, sl=YAR_ENTRY)
    assert (tp, sl) == (YAR_TP, YAR_ENTRY)
    assert sl_lim == round(YAR_ENTRY * 1.005, 1)
    _, _, long_lim = dt.protection_prices("bullish", 10000.0, tp=10340.0, sl=10000.0)
    assert long_lim == round(10000.0 * 0.995, 1)


def test_hourly_repair_keeps_entry_stop_only_when_flagged():
    flagged = dict(PRIMARY, sl_breakeven=True)
    tp, sl = dt.breakeven_replace_args(flagged)
    prices = dt.protection_prices(flagged["direction"], flagged["entry_price"], tp=tp, sl=sl)
    assert prices[0] == 75186.0
    assert prices[1] == 77832.3
    assert prices[1] != dt.protection_prices("bearish", 77832.3)[1]


def test_cancel_ids_include_position_level_orders():
    snap = {"orders": [{"orderId": "a"}], "position": {"slOrderId": "b", "tpOrderId": "a"}}
    assert dt.tpsl_order_ids(snap) == ["a", "b"]
    assert dt.tpsl_trigger_prices(
        {"position": {"sl_trigger_price": 79388.9}, "orders": []}, "sl") == [79388.9]


def test_position_match_accepts_the_open_shorts_and_rejects_drift():
    live = {"size": -0.01285, "entry_price": 77832.3, "markPrice": 77100.0}
    assert dt.position_match_problems(PRIMARY, live) == []
    # one tenth of entry is the recorded tick; a dollar is a different position
    assert dt.position_match_problems(PRIMARY, dict(live, entry_price=77832.4)) == []
    assert dt.position_match_problems(PRIMARY, dict(live, entry_price=77833.3))
    assert dt.position_match_problems(PRIMARY, dict(live, size=-0.01286))
    assert dt.position_match_problems(PRIMARY, dict(live, size=0.01285))
    assert dt.position_match_problems(PRIMARY, None) == ["no live BTC position"]
    assert dt.position_match_problems(None, live) == ["no open position in state"]


def test_short_breakeven_requires_mark_below_entry():
    assert dt.breakeven_stop_valid("bearish", 77100.0, 77832.3)
    assert not dt.breakeven_stop_valid("bearish", 77832.3, 77832.3)
    assert not dt.breakeven_stop_valid("bearish", 78000.0, 77832.3)
    assert dt.breakeven_stop_valid("bullish", 11000.0, 10000.0)
    assert not dt.breakeven_stop_valid("bullish", 9000.0, 10000.0)


def _run(tmp_path, live, handler, equity=158.0, dry_run=False):
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "log.json"
    state_path.write_text(json.dumps({"position": dict(PRIMARY), "acted": {}}))
    saved = (dt.STATE_FILE, dt.LOG_FILE, dt.DRY_RUN, dt.run_cli, dt.time.sleep, dt.tg)
    calls = []

    def run_cli(action, params):
        calls.append((action, params))
        return handler(action, params, calls)

    dt.STATE_FILE = str(state_path)
    dt.LOG_FILE = str(log_path)
    dt.DRY_RUN = dry_run
    dt.run_cli = run_cli
    dt.time.sleep = lambda _s: None
    dt.tg = lambda _msg: None
    try:
        code = dt.move_sl_to_breakeven(json.loads(state_path.read_text()), live, equity)
        return code, calls, json.loads(state_path.read_text())
    finally:
        dt.STATE_FILE, dt.LOG_FILE, dt.DRY_RUN, dt.run_cli, dt.time.sleep, dt.tg = saved


def _before():
    return {
        "position": {
            "tpTriggerPrice": 75186.0, "tpLimitPrice": 75186.0,
            "slTriggerPrice": 79388.9, "slLimitPrice": 79785.9,
            "tpOrderId": "tp1", "slOrderId": "sl1",
        },
        "orders": [
            {"orderId": "tp1", "tpTriggerPrice": 75186.0},
            {"orderId": "sl1", "slTriggerPrice": 79388.9},
        ],
    }


def _after():
    return {
        "position": {
            "tpTriggerPrice": 75186.0, "slTriggerPrice": 77832.3,
            "tpOrderId": "tp2", "slOrderId": "sl2",
        },
        "orders": [{"orderId": "tp2"}, {"orderId": "sl2", "slTriggerPrice": 77832.3}],
    }


def test_move_refuses_a_different_position_without_sending_orders(tmp_path):
    live = {"size": -0.02, "entry_price": 77832.3, "markPrice": 77100.0}
    code, calls, st = _run(tmp_path, live, lambda *a: (_ for _ in ()).throw(AssertionError("cli")))
    assert code == 2
    assert calls == []
    assert st["position"]["sl_price"] == 79388.9
    assert "sl_breakeven" not in st["position"]


def test_move_refuses_when_mark_is_through_entry(tmp_path):
    live = {"size": -0.01285, "entry_price": 77832.3, "markPrice": 77900.0}
    code, calls, st = _run(tmp_path, live, lambda *a: (_ for _ in ()).throw(AssertionError("cli")))
    assert code == 2
    assert calls == []
    assert st["position"]["size"] == 0.01285


def test_dry_run_prints_the_plan_and_does_not_replace(tmp_path):
    live = {"size": -0.01285, "entry_price": 77832.3, "markPrice": 77100.0}

    def handler(action, params, calls):
        assert action == "get_tp_sl"
        return _before()

    code, calls, st = _run(tmp_path, live, handler, dry_run=True)
    assert code == 0
    assert [c[0] for c in calls] == ["get_tp_sl"]
    assert st["position"]["sl_price"] == 79388.9
    assert "sl_breakeven" not in st["position"]


def test_move_replaces_sl_at_entry_and_keeps_tp(tmp_path):
    live = {"size": -0.01285, "entry_price": 77832.3, "markPrice": 77100.0}

    def handler(action, params, calls):
        if action == "get_tp_sl":
            if any(a == "place_tp_sl" for a, _ in calls):
                return _after()
            return _before()
        if action == "cancel_tp_sl":
            return {"success": True}
        if action == "place_tp_sl":
            return {"success": True, "transactionHash": "0xabc"}
        raise AssertionError(action)

    code, calls, st = _run(tmp_path, live, handler)
    actions = [c[0] for c in calls]
    assert code == 0
    assert "place_market_order" not in actions
    assert "close_position" not in actions
    assert "set_leverage" not in actions
    placed = [p for a, p in calls if a == "place_tp_sl"]
    assert len(placed) == 1
    assert placed[0]["tpTriggerPrice"] == 75186.0
    assert placed[0]["tpLimitPrice"] == 75186.0
    assert placed[0]["slTriggerPrice"] == 77832.3
    assert placed[0]["slLimitPrice"] == round(77832.3 * 1.005, 1)
    cancelled = {p["orderId"] for a, p in calls if a == "cancel_tp_sl"}
    assert cancelled == {"tp1", "sl1"}
    pos = st["position"]
    assert pos["sl_breakeven"] is True
    assert pos["sl_price"] == 77832.3
    assert pos["tp_price"] == 75186.0
    assert pos["tp_sl_ok"] is True
    assert pos["size"] == 0.01285
    assert pos["entry_price"] == 77832.3
    log = json.loads((tmp_path / "log.json").read_text())
    assert log[-1]["action"] == "MOVE_SL_BREAKEVEN"
    assert log[-1]["result"] == "OK"
    assert log[-1]["old_sl"] == 79388.9


def test_unconfirmed_place_keeps_the_breakeven_flag(tmp_path):
    live = {"size": -0.01285, "entry_price": 77832.3, "markPrice": 77100.0}

    def handler(action, params, calls):
        if action == "get_tp_sl":
            return _before()  # confirm never shows the new stop
        if action == "cancel_tp_sl":
            return {"success": True}
        if action == "place_tp_sl":
            return {"success": True}
        raise AssertionError(action)

    code, _, st = _run(tmp_path, live, handler)
    assert code == 1
    assert st["position"]["sl_breakeven"] is True
    assert st["position"]["sl_price"] == 77832.3
    assert st["position"]["tp_sl_ok"] is False
    assert st["position"]["size"] == 0.01285
