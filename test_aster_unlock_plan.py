#!/usr/bin/env python3
"""Unit tests for ASTER unlock sizing, tranche selection, and idempotency."""
import unittest
from datetime import datetime, timezone

from aster_unlock_plan import (
    CLOSE_STOP_PX,
    ENTRY_CRONS,
    EXIT_CRONS,
    LEVERAGE,
    MANAGE_UNTIL,
    MARGIN_BUFFER,
    MIN_ORDER_NOTIONAL_USD,
    SL_BACKSTOP_PX,
    STOP_CRON,
    TIME_EXIT,
    TP1_PX,
    TP2_PX,
    apply_fill,
    campaign_short,
    build_protection,
    decide_check_stop,
    decide_close_all,
    decide_entry,
    empty_state,
    floor_sz,
    isolated_short_liq_price,
    latest_closed_4h,
    live_trading_enabled,
    make_cloid,
    mark_exit,
    mark_tranche_filled,
    notional_cap,
    protection_matches,
    resolve_action,
    round_px,
    schedule_in_window,
    split_tp_sizes,
    tranche_notional,
)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


MARK = 0.71271
MID = 0.712685
ACCOUNT = 10_000.0


def entry(**overrides):
    args = dict(
        now=utc(2026, 10, 29, 12, 5),
        state=empty_state(),
        account_value=ACCOUNT,
        free_margin=ACCOUNT,
        mark=MARK,
        mid=MID,
        position_sz=0.0,
        sz_decimals=0,
        max_leverage=5,
        stop_already_triggered=False,
    )
    args.update(overrides)
    return decide_entry(**args)


class SizingTests(unittest.TestCase):
    def test_cap_is_ten_percent_at_two_times(self):
        self.assertEqual(notional_cap(10_000), 2_000)
        self.assertAlmostEqual(tranche_notional(10_000), 2_000 / 3)

    def test_tranche_floors_to_whole_aster_and_three_fit_under_the_cap(self):
        decision = entry()
        sz = decision["entry_order"]["sz"]
        self.assertEqual(sz, floor_sz(tranche_notional(ACCOUNT) / MARK, 0))
        self.assertEqual(sz, 935)
        self.assertLessEqual(sz * MARK, tranche_notional(ACCOUNT))
        self.assertGreater((sz + 1) * MARK, tranche_notional(ACCOUNT))
        self.assertLessEqual(3 * sz * MARK, notional_cap(ACCOUNT))
        self.assertAlmostEqual(decision["sizing"]["margin_usd"] * LEVERAGE, sz * MARK)
        self.assertFalse(decision["entry_order"]["reduce_only"])
        self.assertFalse(decision["entry_order"]["is_buy"])
        self.assertEqual(decision["entry_order"]["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertLess(decision["entry_order"]["limit_px"], MID)

    def test_notional_cap_shrinks_the_order_and_never_exceeds_it(self):
        live_sz = 1500 / MARK
        decision = entry(position_sz=-live_sz, state=_filled("2026-10-29", live_sz),
                         now=utc(2026, 11, 1, 12, 5))
        self.assertTrue(decision["send"])
        added = decision["entry_order"]["sz"]
        self.assertLess(added * MARK, tranche_notional(ACCOUNT))
        self.assertLessEqual(1500 + added * MARK, notional_cap(ACCOUNT) + 1e-6)
        self.assertEqual(decision["sizing"]["binding"], "notional_cap")

    def test_free_margin_caps_notional(self):
        decision = entry(free_margin=100)
        notional = decision["entry_order"]["sz"] * MARK
        self.assertLessEqual(notional, 100 * LEVERAGE * MARGIN_BUFFER + 1e-6)
        self.assertEqual(decision["sizing"]["binding"], "free_margin")

    def test_below_exchange_minimum_is_not_sent(self):
        decision = entry(account_value=100, free_margin=100)
        self.assertFalse(decision["send"])
        self.assertEqual(decision["reason"], "below_min_notional")
        self.assertIsNone(decision["entry_order"])
        self.assertLess(tranche_notional(100), MIN_ORDER_NOTIONAL_USD)

    def test_no_free_margin(self):
        decision = entry(free_margin=0)
        self.assertFalse(decision["send"])
        self.assertEqual(decision["reason"], "no_free_margin")


class PriceGridTests(unittest.TestCase):
    def test_official_perp_examples(self):
        # 5 significant figures, then at most 6 decimals when szDecimals is 0.
        self.assertEqual(round_px(1234.56, 0), 1234.6)
        self.assertEqual(round_px(0.0012345, 0), 0.001234)
        # szDecimals 1 leaves 5 decimal places. 0.012345 is one place too fine.
        # The SDK rounds half away via float, which lands on 0.01235 here.
        self.assertEqual(round_px(0.012345, 1), 0.01235)
        self.assertNotEqual(round_px(0.012345, 1), 0.012345)
        self.assertEqual(round_px(0.01234, 1), 0.01234)

    def test_plan_prices_are_on_the_grid(self):
        for px in (TP1_PX, TP2_PX, CLOSE_STOP_PX, SL_BACKSTOP_PX):
            self.assertEqual(round_px(px, 0), px)

    def test_trigger_limits_are_above_triggers(self):
        protection = build_protection(100, MARK, 0)
        by_purpose = {order["purpose"]: order for order in protection["triggers"]}
        self.assertEqual(set(by_purpose), {"tp1", "tp2", "sl"})
        self.assertEqual(by_purpose["tp1"]["order_type"]["trigger"]["triggerPx"], TP1_PX)
        self.assertEqual(by_purpose["tp2"]["order_type"]["trigger"]["triggerPx"], TP2_PX)
        self.assertEqual(by_purpose["sl"]["order_type"]["trigger"]["triggerPx"], SL_BACKSTOP_PX)
        for order in protection["triggers"]:
            self.assertTrue(order["reduce_only"])
            self.assertTrue(order["is_buy"])
            self.assertTrue(order["order_type"]["trigger"]["isMarket"])
            self.assertGreater(order["limit_px"], order["order_type"]["trigger"]["triggerPx"])
            wire = f"{order['limit_px']:.8f}"
            self.assertLess(abs(float(wire) - order["limit_px"]), 1e-12)


class SplitAndProtectionTests(unittest.TestCase):
    def test_half_and_remainder(self):
        self.assertEqual(split_tp_sizes(100, 0), (50, 50))
        self.assertEqual(split_tp_sizes(101, 0), (50, 51))
        self.assertEqual(split_tp_sizes(1, 0), (1, 0))
        self.assertEqual(split_tp_sizes(0, 0), (0, 0))

    def test_protection_sizes_sum_to_the_position(self):
        protection = build_protection(101, MARK, 0)
        tps = [o for o in protection["triggers"] if o["order_type"]["trigger"]["tpsl"] == "tp"]
        sl = [o for o in protection["triggers"] if o["order_type"]["trigger"]["tpsl"] == "sl"]
        self.assertEqual(sum(o["sz"] for o in tps), 101)
        self.assertEqual(sl[0]["sz"], 101)
        self.assertEqual([o["sz"] for o in tps], [50, 51])

    def test_mark_through_first_target_closes_that_half_immediately(self):
        protection = build_protection(100, 0.65, 0)
        self.assertEqual(protection["immediate"][0]["sz"], 50)
        self.assertTrue(protection["immediate"][0]["reduce_only"])
        purposes = [o["purpose"] for o in protection["triggers"]]
        self.assertNotIn("tp1", purposes)
        self.assertIn("tp2", purposes)
        self.assertIn("sl", purposes)

    def test_mark_through_backstop_closes_the_whole_position(self):
        protection = build_protection(80, 0.90, 0)
        self.assertEqual(protection["triggers"], [])
        self.assertEqual(protection["immediate"][0]["sz"], 80)
        self.assertTrue(protection["immediate"][0]["reduce_only"])

    def test_cloids_are_stable_and_valid(self):
        self.assertEqual(make_cloid("tp1"), make_cloid("tp1"))
        self.assertNotEqual(make_cloid("tp1"), make_cloid("tp2"))
        self.assertNotEqual(make_cloid("entry", "2026-10-29:0"), make_cloid("entry", "2026-10-29:10"))
        for purpose in ("tp1", "tp2", "sl"):
            cloid = make_cloid(purpose)
            self.assertTrue(cloid.startswith("0x"))
            self.assertEqual(len(cloid), 34)


class IdempotencyTests(unittest.TestCase):
    def test_repeat_of_a_filled_tranche_does_not_send_another(self):
        first = entry()
        state = apply_fill(empty_state(), "2026-10-29", first["target_sz"], first["entry_order"]["sz"],
                           utc(2026, 10, 29, 12, 6), MARK)
        self.assertEqual(state["tranches"]["2026-10-29"]["status"], "filled")
        again = entry(now=utc(2026, 10, 29, 16, 5), state=state, position_sz=-first["entry_order"]["sz"])
        self.assertFalse(again["send"])
        self.assertEqual(again["reason"], "not_due")
        self.assertEqual(again["tranche_id"], "2026-11-01")

    def test_partial_fill_tops_up_only_the_remainder(self):
        state = apply_fill(empty_state(), "2026-10-29", 100, 40, utc(2026, 10, 29, 12, 6), MARK)
        self.assertEqual(state["tranches"]["2026-10-29"]["status"], "partial")
        decision = entry(state=state, position_sz=-40)
        self.assertTrue(decision["send"])
        self.assertEqual(decision["tranche_id"], "2026-10-29")
        self.assertEqual(decision["entry_order"]["sz"], 60)
        topped = apply_fill(state, "2026-10-29", decision["target_sz"], 60, utc(2026, 10, 29, 12, 7), MARK)
        self.assertEqual(topped["tranches"]["2026-10-29"]["status"], "filled")
        self.assertEqual(topped["tranches"]["2026-10-29"]["filled_sz"], 100)
        third = entry(state=topped, position_sz=-100)
        self.assertNotEqual(third["tranche_id"], "2026-10-29")

    def test_zero_delta_does_not_increase_recorded_size(self):
        state = apply_fill(empty_state(), "2026-10-29", 100, 40, utc(2026, 10, 29, 12, 6), MARK)
        again = apply_fill(state, "2026-10-29", 100, 0, utc(2026, 10, 29, 12, 7), MARK)
        self.assertEqual(again["tranches"]["2026-10-29"]["filled_sz"], 40)

    def test_take_profit_reducing_the_position_does_not_refill_the_tranche(self):
        state = apply_fill(empty_state(), "2026-10-29", 100, 100, utc(2026, 10, 29, 12, 6), MARK)
        decision = entry(now=utc(2026, 11, 1, 12, 5), state=state, position_sz=-50)
        self.assertTrue(decision["send"])
        self.assertEqual(decision["tranche_id"], "2026-11-01")
        self.assertEqual(decision["entry_order"]["sz"], 935)

    def test_remainder_under_minimum_completes_the_tranche_without_an_order(self):
        state = apply_fill(empty_state(), "2026-10-29", 100, 99, utc(2026, 10, 29, 12, 6), MARK)
        decision = entry(state=state, position_sz=-99)
        self.assertFalse(decision["send"])
        self.assertTrue(decision["complete_tranche"])
        self.assertEqual(decision["reason"], "remainder_below_min_notional")
        done = mark_tranche_filled(state, "2026-10-29", utc(2026, 10, 29, 12, 7), decision["reason"])
        nxt = entry(now=utc(2026, 11, 1, 12, 5), state=done, position_sz=-99)
        self.assertEqual(nxt["tranche_id"], "2026-11-01")
        self.assertTrue(nxt["send"])

    def test_unrecognized_short_stands_down(self):
        decision = entry(position_sz=-10)
        self.assertEqual(decision["kind"], "stand_down")
        self.assertEqual(decision["reason"], "unrecognized_short")
        self.assertFalse(decision["send"])
        self.assertIsNone(decision["entry_order"])

    def test_position_larger_than_recorded_stands_down(self):
        state = apply_fill(empty_state(), "2026-10-29", 100, 100, utc(2026, 10, 29, 12, 6), MARK)
        decision = entry(now=utc(2026, 11, 1, 12, 5), state=state, position_sz=-120)
        self.assertEqual(decision["reason"], "position_larger_than_recorded")
        self.assertFalse(decision["send"])

    def test_long_position_stands_down(self):
        self.assertEqual(entry(position_sz=5)["reason"], "long_position_open")

    def test_exit_blocks_a_later_tranche(self):
        state = mark_exit(empty_state(), "4h_close_above_0.785", utc(2026, 11, 2, 16, 10), 0.79)
        decision = entry(now=utc(2026, 11, 3, 12, 5), state=state)
        self.assertEqual(decision["reason"], "campaign_already_exited")
        self.assertFalse(decision["send"])

    def test_not_due_preview_shows_orders_but_does_not_send(self):
        decision = entry(now=utc(2026, 10, 26, 15, 28))
        self.assertEqual(decision["kind"], "preview")
        self.assertEqual(decision["reason"], "not_due")
        self.assertFalse(decision["send"])
        self.assertFalse(decision["eligible_now"])
        self.assertEqual(decision["tranche_id"], "2026-10-29")
        self.assertEqual(decision["entry_order"]["sz"], 935)
        self.assertIsNotNone(decision["leverage"])
        self.assertFalse(decision["leverage"]["isCross"])
        self.assertEqual(decision["leverage"]["leverage"], 2)

    def test_all_three_filled_never_adds_a_fourth(self):
        state = empty_state()
        position = 0.0
        for tid, due in (("2026-10-29", utc(2026, 10, 29, 12, 5)),
                         ("2026-11-01", utc(2026, 11, 1, 12, 5)),
                         ("2026-11-03", utc(2026, 11, 3, 12, 5))):
            decision = entry(now=due, state=state, position_sz=-position)
            self.assertTrue(decision["send"], tid)
            self.assertEqual(decision["tranche_id"], tid)
            state = apply_fill(state, tid, decision["target_sz"], decision["entry_order"]["sz"], due, MARK)
            position += decision["entry_order"]["sz"]
        self.assertLessEqual(position * MARK, notional_cap(ACCOUNT) + 1e-6)
        extra = entry(now=utc(2026, 11, 3, 16, 5), state=state, position_sz=-position)
        self.assertEqual(extra["reason"], "all_tranches_filled")
        self.assertFalse(extra["send"])

    def test_stop_already_triggered_blocks_entry(self):
        decision = entry(stop_already_triggered=True)
        self.assertEqual(decision["reason"], "stop_already_triggered")
        self.assertFalse(decision["send"])

    def test_past_time_exit_blocks_entry(self):
        decision = entry(now=utc(2026, 11, 5, 12, 0))
        self.assertEqual(decision["reason"], "past_time_exit")

    def test_max_leverage_guard(self):
        self.assertEqual(entry(max_leverage=5)["kind"], "enter")
        self.assertEqual(entry(max_leverage=1)["reason"], "max_leverage_below_plan")


class StopAndScheduleTests(unittest.TestCase):
    def test_close_above_stop_triggers_and_a_wick_does_not(self):
        now = utc(2026, 11, 2, 16, 10)
        closed_above = _candle(utc(2026, 11, 2, 12, 0), close=0.790, high=0.80)
        wick = _candle(utc(2026, 11, 2, 12, 0), close=0.770, high=0.90)
        forming = _candle(utc(2026, 11, 2, 16, 0), close=0.99, high=1.0)
        self.assertTrue(latest_closed_4h([closed_above, forming], now)["stop"])
        wick_result = latest_closed_4h([wick, forming], now)
        self.assertFalse(wick_result["stop"])
        self.assertEqual(wick_result["close"], 0.770)
        equal = _candle(utc(2026, 11, 2, 12, 0), close=CLOSE_STOP_PX, high=0.80)
        self.assertFalse(latest_closed_4h([equal], now)["stop"])

    def test_forming_candle_is_ignored(self):
        now = utc(2026, 11, 2, 16, 10)
        previous = _candle(utc(2026, 11, 2, 12, 0), close=0.70, high=0.71)
        forming = _candle(utc(2026, 11, 2, 16, 0), close=0.99, high=1.0)
        result = latest_closed_4h([previous, forming], now)
        self.assertEqual(result["close"], 0.70)
        self.assertFalse(result["stop"])

    def test_check_stop_closes_only_the_recorded_short_and_is_reduce_only(self):
        now = utc(2026, 11, 2, 16, 10)
        state = apply_fill(empty_state(), "2026-10-29", 100, 100, utc(2026, 10, 29, 12, 6), MARK)
        candle = _candle(utc(2026, 11, 2, 12, 0), close=0.79, high=0.80)
        decision = decide_check_stop(
            now=now, candles=[candle], position_sz=-100, mark=0.79, mid=0.79,
            sz_decimals=0, state=state,
        )
        self.assertTrue(decision["send"])
        self.assertEqual(decision["reason"], "4h_close_above_0.785")
        self.assertTrue(decision["orders"][0]["reduce_only"])
        self.assertTrue(decision["orders"][0]["is_buy"])
        self.assertEqual(decision["orders"][0]["sz"], 100)

        held = decide_check_stop(
            now=now, candles=[_candle(utc(2026, 11, 2, 12, 0), close=0.70, high=0.90)],
            position_sz=-100, mark=0.70, mid=0.70, sz_decimals=0, state=state,
        )
        self.assertEqual(held["kind"], "protect")
        self.assertFalse(held["send"])

        stranger = decide_check_stop(
            now=now, candles=[candle], position_sz=-100, mark=0.79, mid=0.79,
            sz_decimals=0, state=empty_state(),
        )
        self.assertEqual(stranger["kind"], "stand_down")
        self.assertFalse(stranger["send"])
        self.assertEqual(stranger["orders"], [])
        self.assertIsNone(campaign_short(empty_state(), -100, 0))

    def test_time_exit_on_the_stop_check_and_on_close_all(self):
        now = utc(2026, 11, 5, 12, 5)
        state = apply_fill(empty_state(), "2026-10-29", 40, 40, utc(2026, 10, 29, 12, 6), MARK)
        decision = decide_check_stop(
            now=now, candles=[_candle(utc(2026, 11, 5, 8, 0), close=0.70, high=0.71)],
            position_sz=-40, mark=0.70, mid=0.70, sz_decimals=0, state=state,
        )
        self.assertEqual(decision["reason"], "time_exit")
        self.assertTrue(decision["orders"][0]["reduce_only"])
        manual = decide_close_all(position_sz=-40, mid=0.70, sz_decimals=0)
        self.assertTrue(manual["send"])
        self.assertTrue(manual["orders"][0]["reduce_only"])
        flat = decide_close_all(position_sz=0, mid=0.70, sz_decimals=0)
        self.assertFalse(flat["send"])

    def test_live_gate(self):
        self.assertFalse(live_trading_enabled("", "schedule", ""))
        self.assertFalse(live_trading_enabled("false", "schedule", ""))
        self.assertFalse(live_trading_enabled("TRUE", "schedule", ""))
        self.assertFalse(live_trading_enabled(" true ", "workflow_dispatch", "true"))
        self.assertFalse(live_trading_enabled("true", "workflow_dispatch", "true"))
        self.assertFalse(live_trading_enabled("true", "workflow_dispatch", ""))
        self.assertFalse(live_trading_enabled("true", "push", "false"))
        self.assertTrue(live_trading_enabled(" true ", "schedule", ""))
        self.assertTrue(live_trading_enabled("true", "workflow_dispatch", "false"))

    def test_cron_resolution_and_window(self):
        self.assertEqual(resolve_action("", "5 12 29 10 *", utc(2026, 10, 29, 12, 5)), "enter_tranche")
        self.assertEqual(resolve_action("", "5 16 3 11 *", utc(2026, 11, 3, 16, 5)), "enter_tranche")
        self.assertEqual(resolve_action("", STOP_CRON, utc(2026, 11, 2, 4, 10)), "check_stop")
        self.assertEqual(resolve_action("", "5 12 5 11 *", utc(2026, 11, 5, 12, 5)), "close_all")
        self.assertIn("5 12 1 11 *", ENTRY_CRONS)
        self.assertIn("5 16 5 11 *", EXIT_CRONS)
        self.assertEqual(resolve_action("status", STOP_CRON, utc(2026, 11, 2, 4, 10)), "status")
        self.assertEqual(resolve_action("", "not a cron", utc(2026, 10, 29, 13, 0)), "enter_tranche")
        self.assertEqual(resolve_action("", "not a cron", utc(2026, 11, 4, 8, 0)), "check_stop")
        self.assertTrue(schedule_in_window("enter_tranche", utc(2026, 10, 29, 12, 5), "schedule"))
        self.assertFalse(schedule_in_window("enter_tranche", utc(2027, 10, 29, 12, 5), "schedule"))
        self.assertFalse(schedule_in_window("check_stop", utc(2026, 10, 1, 0, 10), "schedule"))
        self.assertTrue(schedule_in_window("check_stop", MANAGE_UNTIL, "schedule"))
        self.assertFalse(schedule_in_window("check_stop", utc(2026, 11, 8, 0, 1), "schedule"))
        self.assertTrue(schedule_in_window("close_all", utc(2026, 12, 1), "workflow_dispatch"))

    def test_backstop_is_beyond_the_september_wick_and_inside_liquidation(self):
        self.assertGreater(SL_BACKSTOP_PX, 0.82049)
        self.assertGreater(SL_BACKSTOP_PX, CLOSE_STOP_PX)
        for entry_px in (0.66, 0.70, 0.71271, 0.75, 0.78):
            liq = isolated_short_liq_price(entry_px, 2, 5)
            self.assertGreater(liq, SL_BACKSTOP_PX)
        # The published formula at a 0.71 entry, 2x isolated, 5x max leverage.
        self.assertAlmostEqual(isolated_short_liq_price(0.71, 2, 5), 0.9681818181, places=6)

    def test_script_isolates_margin_and_reduce_only_exits(self):
        with open("aster_unlock_short.py") as handle:
            text = handle.read()
        self.assertIn("is_cross=False", text)
        self.assertIn("reduce_only=True", text)
        self.assertIn('grouping="na"', text)
        self.assertIn("update_leverage(LEVERAGE, SYMBOL, is_cross=False)", text)

    def test_workflow_file_keeps_the_live_gate_and_dry_run_default(self):
        with open(".github/workflows/aster_unlock_short.yml") as handle:
            text = handle.read()
        self.assertIn("vars.ASTER_LIVE", text)
        self.assertIn("default: true", text)
        self.assertIn("inputs.dry_run && 'true' || 'false'", text)
        self.assertIn("secrets.HYPERLIQUID_API_PRIVATE_KEY", text)
        self.assertIn("secrets.HYPERLIQUID_ACCOUNT_ADDRESS", text)
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", text)
        self.assertNotIn("ASTER_LIVE: 'true'", text)
        self.assertNotIn('ASTER_LIVE: "true"', text)

    def test_resting_protection_matches_on_trigger_price_and_size(self):
        protection = build_protection(100, MARK, 0)
        resting = []
        for order in protection["triggers"]:
            resting.append({
                "coin": "ASTER",
                "isTrigger": True,
                "triggerPx": str(order["order_type"]["trigger"]["triggerPx"]),
                "sz": str(order["sz"]),
                "side": "B",
                "reduceOnly": True,
            })
        self.assertTrue(protection_matches(resting, protection, 0))
        resting[0]["sz"] = "49"
        self.assertFalse(protection_matches(resting, protection, 0))


def _filled(tid, sz):
    state = apply_fill(empty_state(), tid, sz, sz, utc(2026, 10, 29, 12, 6), MARK)
    return state


def _candle(open_time, close, high):
    start_ms = int(open_time.timestamp() * 1000)
    return {
        "t": start_ms,
        "T": start_ms + 4 * 3600 * 1000 - 1,
        "o": "0.70",
        "c": str(close),
        "h": str(high),
        "l": "0.60",
    }


if __name__ == "__main__":
    unittest.main()
