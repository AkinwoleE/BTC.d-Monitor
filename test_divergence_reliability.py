#!/usr/bin/env python3
"""Catch-up, retry, idempotency, and durable-file tests.

Imports only divergence_reliability — not the monitor or the trader, which
pull in HTTP and the exchange CLI.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import divergence_reliability as rel


def _ep(eid, tf, direction, confirmed_ts, price=100.0):
    return {
        "id": eid,
        "tf": tf,
        "direction": direction,
        "confirmed_ts": confirmed_ts,
        "confirm_price": price,
    }


class CatchUpTests(unittest.TestCase):
    def test_closed_since_includes_gap_and_skips_scanned_and_forming(self):
        candles = [
            {"ts": 0},
            {"ts": 7200},
            {"ts": 14400},
            {"ts": 21600},
        ]
        # 21600 closes at 28800, so it is still forming.
        now = 21600 + 100
        missed = rel.closed_since(candles, 0, now, 7200)
        self.assertEqual([c["ts"] for c in missed], [7200, 14400])
        self.assertEqual(rel.closed_since(candles, 14400, now, 7200), [])
        self.assertEqual(rel.closed_since(candles, None, now, 7200), [])

    def test_ten_hour_old_2h_signal_is_still_entered(self):
        now = 1_700_000_000
        ep = _ep("2h-bullish-late", "2h", "bullish", now - 10 * 3600)
        decision = rel.classify_entry(
            ep, now, 100.0, set(), {"2h"}, set(), 0.034, 0.02)
        self.assertEqual(decision, "enter")

    def test_weeks_old_signal_is_too_old(self):
        now = 1_700_000_000
        ep = _ep("2h-bullish-old", "2h", "bullish", now - 21 * 86400)
        decision = rel.classify_entry(
            ep, now, 100.0, set(), {"2h"}, set(), 0.034, 0.02)
        self.assertEqual(decision, "too_old")
        self.assertFalse(rel.should_alert_skip(ep["confirmed_ts"], now))

    def test_1h_window_stays_two_hours(self):
        now = 1_700_000_000
        fresh = _ep("1h-bullish-fresh", "1h", "bullish", now - 3600)
        stale = _ep("1h-bullish-stale", "1h", "bullish", now - 3 * 3600)
        self.assertEqual(
            rel.classify_entry(fresh, now, 100.0, set(), {"1h"}, set(), 0.034, 0.02),
            "enter")
        self.assertEqual(
            rel.classify_entry(stale, now, 100.0, set(), {"1h"}, set(), 0.034, 0.02),
            "too_old")

    def test_price_ran_ahead_blocks_chase(self):
        now = 1_700_000_000
        ep = _ep("2h-bullish-chase", "2h", "bullish", now - 3600, 100.0)
        # +2% vs a 3.4% target is more than halfway to the target.
        decision = rel.classify_entry(
            ep, now, 102.0, set(), {"2h"}, set(), 0.034, 0.02)
        self.assertEqual(decision, "price_ran_ahead")

    def test_small_adverse_move_still_enters(self):
        now = 1_700_000_000
        long_ep = _ep("2h-bullish-dip", "2h", "bullish", now - 3600, 100.0)
        self.assertEqual(
            rel.classify_entry(long_ep, now, 99.0, set(), {"2h"}, set(), 0.034, 0.02),
            "enter")
        # ~1.3% through a short, same shape as the 2026-09-23 fill. Still in.
        short_ep = _ep("2h-bearish-dip", "2h", "bearish", now - 3600, 100.0)
        self.assertEqual(
            rel.classify_entry(short_ep, now, 101.32, set(), {"2h"}, set(), 0.034, 0.02),
            "enter")

    def test_full_stop_adverse_move_is_skipped(self):
        now = 1_700_000_000
        ep = _ep("2h-bullish-stop", "2h", "bullish", now - 3600, 100.0)
        decision = rel.classify_entry(
            ep, now, 98.0, set(), {"2h"}, set(), 0.034, 0.02)
        self.assertEqual(decision, "price_ran_against")

    def test_weekend_block_uses_confirmation_day_not_run_day(self):
        sunday = datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc)
        ts = int(sunday.timestamp())
        self.assertEqual(rel.utc_weekday(ts), "Sun")
        ep = _ep("2h-bullish-sun", "2h", "bullish", ts, 100.0)
        monday = ts + 12 * 3600  # still inside the 18h window
        decision = rel.classify_entry(
            ep, monday, 100.0, set(), {"2h"}, {"Sat", "Sun"}, 0.034, 0.02)
        self.assertEqual(decision, "blocked_day")

    def test_friday_is_not_blocked(self):
        friday = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        ts = int(friday.timestamp())
        self.assertEqual(rel.utc_weekday(ts), "Fri")
        ep = _ep("2h-bullish-fri", "2h", "bullish", ts, 100.0)
        decision = rel.classify_entry(
            ep, ts + 600, 100.0, set(), {"2h"}, {"Sat", "Sun"}, 0.034, 0.02)
        self.assertEqual(decision, "enter")

    def test_recent_skip_alerts_archive_does_not(self):
        now = 1_700_000_000
        self.assertTrue(rel.should_alert_skip(now - 2 * 86400, now))
        self.assertFalse(rel.should_alert_skip(now - 8 * 86400, now))


class RetryTests(unittest.TestCase):
    def test_retry_sleeps_then_succeeds(self):
        sleeps = []
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timed out talking to the exchange")
            return "ok"

        result = rel.retry_call(fn, attempts=3, base_delay=1.0, sleep=sleeps.append)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_permanent_error_is_not_retried(self):
        sleeps = []
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ValueError("MCP error: invalid size")

        with self.assertRaises(ValueError):
            rel.retry_call(fn, attempts=3, base_delay=1.0, sleep=sleeps.append)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sleeps, [])

    def test_nonce_and_http_are_transient_param_errors_are_not(self):
        self.assertTrue(rel.is_transient_error(RuntimeError("nonce already used")))
        self.assertTrue(rel.is_transient_error(RuntimeError("HTTP 503 from node")))
        self.assertTrue(rel.is_transient_error(
            RuntimeError("No JSON-RPC response for place_market_order")))
        self.assertFalse(rel.is_transient_error(RuntimeError("MCP error: invalid size")))
        self.assertTrue(rel.payload_is_transient({"error": "nonce too low"}))
        self.assertFalse(rel.payload_is_transient({"success": False, "error": "insufficient margin"}))


class IdempotencyTests(unittest.TestCase):
    def _pending(self, sent, age, side="long", size=0.01, eid="ep-1"):
        now = 1_000_000
        return {
            "episode_id": eid,
            "order_sent": sent,
            "side": side,
            "size": size,
            "ts": now - age,
        }

    def test_acted_episode_is_skipped(self):
        now = 1_000_000
        state = {"acted": {"ep-1": now - 10}, "pending_open": None}
        self.assertEqual(rel.plan_order(state, "ep-1", None, now, 1800), "skip")

    def test_other_pending_blocks_a_second_entry(self):
        now = 1_000_000
        state = {"acted": {}, "pending_open": self._pending(True, 10, eid="other")}
        self.assertEqual(rel.plan_order(state, "ep-1", None, now, 1800), "blocked_pending")

    def test_matching_live_position_is_adopted(self):
        now = 1_000_000
        pending = self._pending(True, 10)
        live = {"side": "buy", "size": 0.011}  # within 25%
        self.assertEqual(rel.pending_resolution(pending, live, now, 1800), "adopt")
        state = {"acted": {}, "pending_open": pending}
        self.assertEqual(rel.plan_order(state, "ep-1", live, now, 1800), "adopt")

    def test_sent_order_without_a_position_waits_and_does_not_send(self):
        now = 1_000_000
        pending = self._pending(True, 30)
        self.assertEqual(rel.pending_resolution(pending, None, now, 1800), "wait")
        state = {"acted": {}, "pending_open": pending}
        self.assertEqual(rel.plan_order(state, "ep-1", None, now, 1800), "wait")
        self.assertNotEqual(rel.plan_order(state, "ep-1", None, now, 1800), "send")

    def test_rejected_unsent_order_can_be_retried(self):
        now = 1_000_000
        pending = self._pending(False, 30)
        self.assertEqual(rel.pending_resolution(pending, None, now, 1800), "retry_send")
        state = {"acted": {}, "pending_open": pending}
        self.assertEqual(rel.plan_order(state, "ep-1", None, now, 1800), "send")

    def test_give_up_after_wait_is_not_a_second_send(self):
        now = 1_000_000
        pending = self._pending(True, 5000)
        self.assertEqual(rel.pending_resolution(pending, None, now, 1800), "give_up")
        state = {"acted": {}, "pending_open": pending}
        decision = rel.plan_order(state, "ep-1", None, now, 1800)
        self.assertEqual(decision, "give_up")
        self.assertNotEqual(decision, "send")

    def test_size_outside_tolerance_is_not_adopted(self):
        now = 1_000_000
        pending = self._pending(True, 10, size=0.01)
        live = {"isLong": True, "size": 0.02}
        self.assertEqual(rel.live_side(live), "long")
        self.assertEqual(rel.pending_resolution(pending, live, now, 1800), "wait")

    def test_signed_short_size_matches(self):
        self.assertEqual(rel.live_side({"size": -0.2}), "short")
        self.assertTrue(rel.live_matches({"positionSide": "short", "size": -0.2}, "short", 0.2))


class DurableFileTests(unittest.TestCase):
    def test_corrupt_json_restored_from_bak_and_bytes_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            rel.atomic_write_json(path, {"position": {"episode_id": "keep"}, "acted": {}})
            rel.atomic_write_json(path, {"position": None, "acted": {"x": 1}})
            with open(path + ".bak", encoding="utf-8") as handle:
                backup = json.load(handle)
            self.assertEqual(backup["position"]["episode_id"], "keep")
            corrupt = "{not-json"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(corrupt)
            loaded = rel.read_json_durable(path, default=[])
            self.assertEqual(loaded["position"]["episode_id"], "keep")
            copies = [n for n in os.listdir(tmp) if ".corrupt-" in n]
            self.assertEqual(len(copies), 1)
            with open(os.path.join(tmp, copies[0]), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), corrupt)
            self.assertNotEqual(loaded, [])

    def test_corrupt_without_backup_is_not_wiped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trades.json")
            corrupt = "{broke"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(corrupt)
            with self.assertRaises(rel.DurableJsonError):
                rel.read_json_durable(path, default=[])
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), corrupt)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_corrupt_file_does_not_clobber_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            rel.atomic_write_json(path, {"ok": 1})
            rel.atomic_write_json(path, {"ok": 2})
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            self.assertFalse(rel.backup_if_valid(path))
            with open(path + ".bak", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["ok"], 1)

    def test_checkpoint_disabled_does_not_call_git(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("git should not run")

        original = rel.subprocess.run
        rel.subprocess.run = boom
        try:
            self.assertFalse(rel.flush_git_state(["/tmp/does-not-matter"], enabled=False))
        finally:
            rel.subprocess.run = original


if __name__ == "__main__":
    unittest.main()
