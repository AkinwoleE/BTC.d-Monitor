#!/usr/bin/env python3
"""Offline tests for turtle sizing, candle identity, and order rounding.

No network. Run: python -m unittest test_turtle_hyperliquid.py
"""
import unittest

from hyperliquid.utils.signing import float_to_wire

from turtle_backtest import compute_N
from turtle_equity_lib import bar_is_complete, merge_daily
from turtle_hyperliquid_lib import merge_crypto
from turtle_hyperliquid_trader import (
    apply_token,
    book_paths,
    completed_candles,
    decide,
    executed_fill,
    format_alert,
    html_escape,
    min_account_for_unit,
    parse_order_status,
    process_all,
    reconciliation_action,
    round_px,
    round_sz,
    size_unit,
    stop_price,
    upsert_stop,
)


def _flat(n=80, base=100.0, start=1_700_000_000):
    start -= start % 86400
    out = []
    for i in range(n):
        px = base
        out.append({
            "ts": start + i * 86400,
            "open": px, "high": px + 1, "low": px - 1, "close": px,
        })
    return out


class Fake:
    def __init__(self, live=False, fail_modify=False):
        self.live = live
        self.fail_modify = fail_modify
        self.calls = []

    def ensure_leverage(self, tok):
        self.calls.append(("lev", tok))

    def open_market(self, tok, is_buy, sz):
        self.calls.append(("open", tok, is_buy, sz))
        if not self.live:
            return {"dry_run": True}
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": "111", "oid": 7}}
        ]}}}

    def close_market(self, tok):
        self.calls.append(("close", tok))
        if not self.live:
            return {"dry_run": True}
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": "10", "avgPx": "90", "oid": 8}}
        ]}}}

    def cancel_orders(self, tok):
        self.calls.append(("cancel_all", tok))

    def cancel_oid(self, tok, oid):
        self.calls.append(("cancel", oid))

    def place_stop(self, tok, is_buy, sz, limit_px, trigger_px):
        self.calls.append(("place", tok, is_buy, sz, limit_px, trigger_px))
        if not self.live:
            return {"dry_run": True}
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 99}}]}}}

    def modify_stop(self, tok, oid, is_buy, sz, limit_px, trigger_px):
        self.calls.append(("modify", oid, sz, trigger_px))
        if self.fail_modify:
            raise RuntimeError("reject")
        if not self.live:
            return {"dry_run": True, "oid": oid}
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def fee_for_fill(self, coin, oid):
        return 0.25

    def fills_since(self, tok, since_ts):
        return [{"closedPnl": "-4.5", "fee": "0.1"}]


def _run(tok, candles, state, trades, **kw):
    trader = kw.pop("trader", None) or Fake()
    now = kw.pop("now", candles[-1]["ts"] + 86400 + 12 * 60)
    return apply_token(
        tok, candles, compute_N(candles), state, trades,
        now=now,
        risk_capital=kw.pop("risk_capital", 25_000),
        mids=kw.pop("mids", {tok: 100}),
        pool_tokens=kw.pop("pool_tokens", [tok]),
        trader=trader,
        sz_decimals=kw.pop("sz_decimals", 2),
        excluded=kw.pop("excluded", set()),
        dry_run=kw.pop("dry_run", True),
        live_pos=kw.pop("live_pos", None),
        persist=kw.pop("persist", lambda: None),
        live_known=kw.pop("live_known", True),
    )


class RoundingTests(unittest.TestCase):
    def test_sizes_and_prices_pass_float_to_wire(self):
        prices = (0.012345, 0.07478, 1.234567, 110.76, 356.895, 3858.95,
                  7543.45, 27731.5, 64210.123, 100001.7)
        sizes = (0.044733037539606814, 2.4378e-7, 44.4898, 33.27355, 1.999, 0.1, 0.0)
        for dec in range(0, 6):
            for px in prices:
                float_to_wire(round_px(px, dec))
            for sz in sizes:
                float_to_wire(round_sz(sz, dec))

    def test_size_rounds_down_to_the_lot(self):
        self.assertEqual(round_sz(1.999, 0), 1)
        self.assertEqual(round_sz(1.999, 2), 1.99)
        self.assertEqual(round_sz(2.4378e-7, 0), 0)
        self.assertEqual(round_sz(44.4898, 0), 44)

    def test_size_unit_rejects_zero_capital_and_dust(self):
        sz, why = size_unit(0, 1, 100, 2)
        self.assertIsNone(sz)
        self.assertIn("zero", why)

        # Integer lot (ADA/DOGE/TRX are szDecimals 0) whose notional is under $10.
        sz, why = size_unit(25, 0.01, 0.3, 0)
        self.assertIsNone(sz)
        self.assertIn("minimum", why)

        sz, why = size_unit(250, 0.01, 0.3, 0)
        self.assertEqual(sz, 250)

        # Sub-lot size rounds to 0.
        sz, why = size_unit(1, 50, 100, 2)
        self.assertIsNone(sz)
        self.assertIn("rounds to 0", why)

    def test_min_account_formula(self):
        ratio = 0.12778  # about ZEC's N/price on 2026-11-16
        expected = 10 / (0.01 * 0.25 * (1 / ratio) * 0.95)
        self.assertAlmostEqual(min_account_for_unit(ratio), expected, places=6)
        self.assertGreater(expected, 500)
        self.assertLess(expected, 600)


class CandleIdentityTests(unittest.TestCase):
    def test_schedule_window_sees_yesterdays_bar_only(self):
        day = 1_700_000_000
        day -= day % 86400
        now = day + 12 * 60  # 00:12 UTC, the cron-job.org slot
        yesterday = day - 86400
        bars = [
            {"ts": yesterday, "open": 1, "high": 2, "low": 0.5, "close": 1},
            {"ts": day, "open": 1, "high": 2, "low": 0.5, "close": 1},
        ]
        self.assertTrue(bar_is_complete(yesterday, now))
        self.assertFalse(bar_is_complete(day, now))
        self.assertEqual(
            [c["ts"] for c in completed_candles("NVDA", bars, now)], [yesterday])
        self.assertEqual(
            [c["ts"] for c in completed_candles("BTC", bars, now)], [yesterday])
        self.assertLess(now, day + 86400)

    def test_equity_merge_drops_the_open_session_and_refreshes_recent(self):
        day = 1_700_000_000
        day -= day % 86400
        now = day + 12 * 60
        yesterday = day - 86400
        far_ts = yesterday - 10 * 86400
        merged = merge_daily(
            [
                {"ts": far_ts, "open": 1, "high": 1, "low": 1, "close": 9},
                {"ts": yesterday, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            ],
            [
                {"ts": far_ts, "open": 1, "high": 1, "low": 1, "close": 1},
                {"ts": yesterday, "open": 1, "high": 3, "low": 0.5, "close": 2.5},
                {"ts": day, "open": 2, "high": 4, "low": 1, "close": 3},
            ],
            now,
        )
        by = {c["ts"]: c for c in merged}
        self.assertNotIn(day, by)
        self.assertEqual(by[yesterday]["close"], 2.5)
        self.assertEqual(by[far_ts]["close"], 9)

    def test_crypto_merge_drops_the_forming_utc_day(self):
        day = 1_700_000_000
        day -= day % 86400
        now = day + 12 * 60
        yesterday = day - 86400
        closed = {"ts": yesterday, "open": 1, "high": 1, "low": 1, "close": 1}
        forming = {"ts": day, "open": 1, "high": 1, "low": 1, "close": 1}
        merged = merge_crypto([closed], [closed, forming], now)
        self.assertEqual([c["ts"] for c in merged], [yesterday])

    def test_seed_skips_even_a_breakout(self):
        candles = _flat()
        candles[-1]["high"] = max(c["high"] for c in candles[:-1]) + 5
        state = {"positions": {}}
        trades = []
        fake = Fake()
        saves = {"n": 0}

        def persist():
            saves["n"] += 1

        result = _run("BTC", candles, state, trades, trader=fake, persist=persist)
        self.assertTrue(result["seed"])
        self.assertEqual(state["positions"]["BTC"]["units"], [])
        self.assertEqual(state["positions"]["BTC"]["last_candle_ts"], candles[-1]["ts"])
        self.assertEqual(saves["n"], 1)
        self.assertFalse(any(c[0] == "open" for c in fake.calls))

        _run("BTC", candles, state, trades, trader=fake, persist=persist)
        self.assertEqual(state["positions"]["BTC"]["units"], [])
        self.assertEqual(saves["n"], 1)

    def test_next_candle_opens_at_the_channel_in_paper(self):
        candles = _flat()
        state = {"positions": {}}
        trades = []
        _run("BTC", candles, state, trades)
        hi = max(c["high"] for c in candles[-55:]) + 5
        candles.append({
            "ts": candles[-1]["ts"] + 86400,
            "open": hi - 1, "high": hi, "low": hi - 2, "close": hi - 1,
        })
        _run("BTC", candles, state, trades)
        units = state["positions"]["BTC"]["units"]
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["side"], "long")
        self.assertAlmostEqual(units[0]["entry_px"], 101)
        self.assertNotIn("fee", units[0])
        self.assertGreater(units[0]["sz"], 0)

    def test_same_candle_cannot_stop_out_or_pyramid_twice(self):
        candles = _flat()
        prev = candles[-2]["ts"]
        candles[-1]["low"] = 1
        pos = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10,
                "n_at_entry": 2, "entry_ts": prev,
            }],
            "last_candle_ts": prev,
        }
        state = {"positions": {"BTC": pos}}
        trades = []
        fake = Fake()
        _run("BTC", candles, state, trades, trader=fake)
        self.assertEqual(pos["units"], [])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "STOP")
        self.assertEqual(trades[0]["pnl"], -40)
        self.assertEqual([c[0] for c in fake.calls], ["close", "cancel_all"])

        _run("BTC", candles, state, trades, trader=fake)
        self.assertEqual(len(trades), 1)
        self.assertEqual(pos["units"], [])

        # The 00:10 run and the 00:12 run share this bar. Once it is marked,
        # the second run must not stop the position the first run opened.
        marked = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10,
                "n_at_entry": 2, "entry_ts": prev,
            }],
            "last_candle_ts": candles[-1]["ts"],
        }
        fresh = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10,
                "n_at_entry": 2, "entry_ts": prev,
            }],
            "last_candle_ts": prev,
        }
        n_series = compute_N(candles)
        self.assertEqual(decide(
            "BTC", candles, n_series, marked, 25_000, {"BTC": 100},
            {"positions": {"BTC": marked}}, ["BTC"], set(), 2,
        )["op"], "already")
        plan = decide(
            "BTC", candles, n_series, fresh, 25_000, {"BTC": 100},
            {"positions": {"BTC": fresh}}, ["BTC"], set(), 2,
        )
        self.assertEqual(plan["op"], "close")
        self.assertEqual(plan["reason"], "STOP")

    def test_pyramid_add_is_once_and_modifies_the_stop(self):
        candles = _flat()
        prev = candles[-2]["ts"]
        candles[-1]["low"] = 100
        candles[-1]["high"] = 1000
        candles[-1]["close"] = 110
        pos = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10,
                "n_at_entry": 2, "entry_ts": prev,
            }],
            "last_candle_ts": prev,
            "stop_oid": 55,
        }
        state = {"positions": {"BTC": pos}}
        fake = Fake()
        _run("BTC", candles, state, [], trader=fake)
        self.assertEqual(len(pos["units"]), 2)
        kinds = [c[0] for c in fake.calls]
        self.assertIn("open", kinds)
        self.assertIn("modify", kinds)
        self.assertNotIn("cancel", kinds)
        _run("BTC", candles, state, [], trader=fake)
        self.assertEqual(len(pos["units"]), 2)

    def test_zero_risk_and_leverage_cap_and_exclude(self):
        candles = _flat()
        candles[-1]["high"] = max(c["high"] for c in candles[:-1]) + 5
        st = {"units": [], "last_candle_ts": candles[-2]["ts"]}
        state = {"positions": {"ETH": st}}
        n_series = compute_N(candles)
        plan = decide("ETH", candles, n_series, st, 0, {"ETH": 100}, state, ["ETH"], set(), 2)
        self.assertEqual(plan["op"], "skip")
        self.assertIn("zero", plan["reason"])

        huge = {"units": [{"side": "long", "entry_px": 100, "sz": 10_000, "n_at_entry": 2, "entry_ts": 1}]}
        state = {"positions": {"BTC": huge, "ETH": st}}
        plan = decide(
            "ETH", candles, n_series, st, 25_000,
            {"BTC": 100, "ETH": 100}, state, ["BTC", "ETH"], set(), 2,
        )
        self.assertEqual(plan["op"], "skip")
        self.assertIn("cap", plan["reason"])

        plan = decide("CL", candles, n_series, st, 25_000, {"CL": 100}, {"positions": {"CL": st}}, ["CL"], {"CL"}, 3)
        self.assertEqual(plan["op"], "hold")
        self.assertIn("excluded", plan["reason"])

    def test_four_units_do_not_grow_a_fifth(self):
        candles = _flat()
        candles[-1]["high"] = 1000
        candles[-1]["low"] = 100
        units = [{
            "side": "long", "entry_px": 100, "sz": 1, "n_at_entry": 2, "entry_ts": 1,
        } for _ in range(4)]
        st = {"units": units, "last_candle_ts": candles[-2]["ts"]}
        plan = decide(
            "BTC", candles, compute_N(candles), st, 25_000, {"BTC": 100},
            {"positions": {"BTC": st}}, ["BTC"], set(), 2,
        )
        self.assertEqual(plan["op"], "hold")
        self.assertEqual(len(st["units"]), 4)


class LiveBookTests(unittest.TestCase):
    def test_books_are_separate_files(self):
        paper = book_paths(True, here="/tmp")
        live = book_paths(False, here="/tmp")
        self.assertTrue(paper[0].endswith("turtle_hyperliquid_state.json"))
        self.assertFalse(paper[0].endswith(".live.json"))
        self.assertTrue(live[0].endswith("turtle_hyperliquid_state.live.json"))
        self.assertNotEqual(paper, live)

    def test_paper_does_not_close_when_the_exchange_is_flat(self):
        candles = _flat()
        prev = candles[-2]["ts"]
        candles[-1]["low"] = 100
        candles[-1]["high"] = 100.4
        candles[-1]["close"] = 100
        pos = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10,
                "n_at_entry": 2, "entry_ts": prev,
            }],
            "last_candle_ts": prev,
        }
        state = {"positions": {"BTC": pos}}
        trades = []
        _run("BTC", candles, state, trades, dry_run=True, live_pos=None)
        self.assertTrue(pos["units"])
        self.assertFalse(any(t.get("reason") == "CLOSED_EXTERNALLY" for t in trades))

    def test_live_external_close_does_not_reenter_that_candle(self):
        candles = _flat()
        candles[-1]["high"] = 1000
        candles[-1]["low"] = 1
        prev = candles[-2]["ts"]
        pos = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10,
                "n_at_entry": 2, "entry_ts": prev,
            }],
        }
        state = {"positions": {"BTC": pos}}
        trades = []
        fake = Fake(live=True)
        _run("BTC", candles, state, trades, trader=fake, dry_run=False, live_pos=None)
        self.assertEqual(pos["units"], [])
        self.assertEqual(trades[0]["reason"], "CLOSED_EXTERNALLY")
        self.assertEqual(trades[0]["pnl"], -4.6)
        self.assertFalse(any(c[0] == "open" for c in fake.calls))
        _run("BTC", candles, state, trades, trader=fake, dry_run=False, live_pos=None)
        self.assertEqual(len(trades), 1)
        self.assertEqual(pos["units"], [])

    def test_missing_snapshot_does_not_flatten_or_consume(self):
        candles = _flat()
        candles[-1]["low"] = 1
        prev = candles[-2]["ts"]
        pos = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10,
                "n_at_entry": 2, "entry_ts": prev,
            }],
            "last_candle_ts": prev,
        }
        state = {"positions": {"BTC": pos}}
        trades = []
        _run("BTC", candles, state, trades, dry_run=False, live_pos=None, live_known=False)
        self.assertEqual(len(pos["units"]), 1)
        self.assertEqual(trades, [])
        self.assertEqual(pos["last_candle_ts"], prev)

    def test_unrecognized_live_position_stands_down(self):
        candles = _flat()
        candles[-1]["high"] = max(c["high"] for c in candles[:-1]) + 5
        pos = {"units": []}
        state = {"positions": {"BTC": pos}}
        fake = Fake(live=True)
        result = _run(
            "BTC", candles, state, [], trader=fake, dry_run=False,
            live_pos={"szi": "2"},
        )
        self.assertEqual(pos["units"], [])
        self.assertNotIn("last_candle_ts", pos)
        self.assertFalse(any(c[0] == "open" for c in fake.calls))
        self.assertTrue(any("Standing down" in m for m in result["messages"]))

    def test_live_open_records_the_fill_and_persists_before_the_stop(self):
        candles = _flat()
        state = {"positions": {}}
        _run("BTC", candles, state, [])  # seed
        hi = max(c["high"] for c in candles[-55:]) + 5
        candles.append({
            "ts": candles[-1]["ts"] + 86400,
            "open": hi - 1, "high": hi, "low": hi - 2, "close": hi - 1,
        })
        saves = {"n": 0}

        class Watch(Fake):
            def place_stop(self, *args, **kwargs):
                self.calls.append(("saw-units", len(state["positions"]["BTC"]["units"])))
                assert saves["n"] >= 1
                return super().place_stop(*args, **kwargs)

        def persist():
            saves["n"] += 1

        fake = Watch(live=True)
        _run("BTC", candles, state, [], trader=fake, dry_run=False, persist=persist)
        unit = state["positions"]["BTC"]["units"][0]
        self.assertEqual(unit["entry_px"], 111)
        self.assertEqual(unit["fee"], 0.25)
        self.assertEqual(state["positions"]["BTC"]["stop_oid"], 99)
        self.assertNotEqual(unit["signal_px"], 111)
        place = next(c for c in fake.calls if c[0] == "place")
        self.assertAlmostEqual(place[5], round_px(111 - 2 * unit["n_at_entry"], 2))
        self.assertIn(("saw-units", 1), fake.calls)

    def test_live_close_uses_the_fill_and_cancels_after(self):
        candles = _flat()
        candles[-1]["low"] = 1
        prev = candles[-2]["ts"]
        pos = {
            "units": [{
                "side": "long", "entry_px": 100, "sz": 10, "n_at_entry": 2,
                "entry_ts": prev, "fee": 0.25,
            }],
            "last_candle_ts": prev,
            "stop_oid": 55,
        }
        state = {"positions": {"BTC": pos}}
        trades = []
        fake = Fake(live=True)
        _run("BTC", candles, state, trades, trader=fake, dry_run=False, live_pos={"szi": "10"})
        self.assertEqual(trades[0]["exit_px"], 90)
        self.assertEqual(trades[0]["pnl"], -100.5)
        self.assertEqual(trades[0]["fees"], 0.5)
        self.assertEqual([c[0] for c in fake.calls], ["close", "cancel_all"])

    def test_reconciliation_matrix(self):
        units = [{"side": "long"}]
        self.assertIsNone(reconciliation_action(True, units, None))
        self.assertEqual(reconciliation_action(False, units, None), "close_external")
        self.assertEqual(reconciliation_action(False, [], {"szi": "1"}), "stand_down")
        self.assertEqual(
            reconciliation_action(False, units, {"szi": "-1"}), "side_mismatch")
        self.assertIsNone(reconciliation_action(False, units, {"szi": "2"}))

    def test_stop_modify_failure_places_the_new_stop_first(self):
        fake = Fake(live=True, fail_modify=True)
        oid = upsert_stop(fake, "BTC", "long", 1.5, 100.0, 55, 2)
        kinds = [c[0] for c in fake.calls]
        self.assertEqual(kinds, ["modify", "place", "cancel"])
        self.assertLess(kinds.index("place"), kinds.index("cancel"))
        self.assertEqual(oid, 99)

        fake = Fake(live=True)
        oid = upsert_stop(fake, "BTC", "long", 1.25, 100.0, 55, 2)
        self.assertEqual([c[0] for c in fake.calls], ["modify"])
        self.assertEqual(oid, 55)
        placed = stop_price([{"side": "long", "entry_px": 100, "n_at_entry": 2}])
        self.assertEqual(placed, 96)

    def test_order_errors_and_fills_parse(self):
        rejected = {"status": "ok", "response": {"data": {"statuses": [
            {"error": "Order must have minimum value of 10 usd."}
        ]}}}
        with self.assertRaises(RuntimeError):
            parse_order_status(rejected)
        filled = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"avgPx": "1.5", "totalSz": "2", "oid": 3}}
        ]}}}
        self.assertEqual(parse_order_status(filled)["px"], 1.5)
        with self.assertRaises(RuntimeError):
            executed_fill(rejected, False, 1, 1)
        paper = executed_fill({"dry_run": True}, True, 10, 2)
        self.assertEqual(paper["px"], 10)

    def test_one_instrument_failing_does_not_stop_the_others(self):
        seen = []

        def handle(tok):
            seen.append(tok)
            if tok == "ETH":
                raise RuntimeError("boom")

        failed = process_all(["BTC", "ETH", "SOL"], handle, lambda tok, err: seen.append("err:" + tok))
        self.assertEqual(seen, ["BTC", "ETH", "err:ETH", "SOL"])
        self.assertEqual(failed, ["ETH"])

    def test_alerts_name_the_book(self):
        paper = format_alert("Turtle/BTC opened", True)
        live = format_alert("Turtle/BTC opened", False)
        self.assertIn("DRY RUN / PAPER", paper)
        self.assertNotIn("DRY RUN", live)
        self.assertIn("LIVE", live)
        self.assertEqual(html_escape("a<b>&"), "a&lt;b&gt;&amp;")

    def test_workflow_pins_the_sdk_and_refreshes_main(self):
        with open(".github/workflows/turtle_hyperliquid.yml") as f:
            text = f.read()
        self.assertIn('hyperliquid-python-sdk==0.24.0', text)
        self.assertIn("git checkout -B main origin/main", text)
        self.assertIn('cron: "10 0 * * *"', text)
        self.assertIn("00:12 UTC", text)
        self.assertIn("8401012", text)
        with open("turtle_hyperliquid_trader.py") as f:
            src = f.read()
        self.assertIn("28 instruments", src)
        self.assertNotIn("insufficient-margin skip", src)
        self.assertIn('PERP_DEXS = ["", "xyz"]', src)


if __name__ == "__main__":
    unittest.main()
