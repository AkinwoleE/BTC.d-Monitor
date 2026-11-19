"""Entry, exit, and risk decisions for the funding hedge. No network."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy
from redact import sanitize

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
NOW = datetime.datetime(2026, 11, 19, 16, 0, 0)


def cfg():
    return strategy.load_config()


def account(**kw):
    base = {
        "dec_equity": 141.0,
        "hl_free": 220.0,
        "hl_mtm": 331.0,
        "equity_ok": True,
        "daily_loss": False,
        "paused": False,
        "paper": True,
        "errors": [],
        "day": "2026-11-19",
        "day_start_equity": 472.0,
    }
    base.update(kw)
    return base


def snap(**kw):
    base = {
        "coin": "ETH",
        "in_overlap": True,
        "tradable": True,
        "dec_sz": 0.0,
        "hl_sz": 0.0,
        "dec_funding_hr": 0.0002,
        "hl_funding_hr": 0.0,
        "dec_oi": 500.0,
        "hl_oi": 5000.0,
        "dec_mid": 100.0,
        "hl_mid": 100.0,
        "dec_mark": 100.0,
        "hl_mark": 100.0,
        "dec_liq": None,
        "hl_liq": None,
        "lot": 0.001,
        "min_size": 0.001,
        "mode": "Open",
    }
    base.update(kw)
    return base


def hours(coin, n, dec, hl, start=0):
    rows = []
    for i in range(n):
        rows.append({"hour": f"2026-11-19T{start + i:02d}", "coin": coin, "dec": dec, "hl": hl})
    return rows


class FundingMath(unittest.TestCase):
    def test_direction_shorts_the_higher_rate(self):
        direction, net = strategy.choose_direction(0.0002, 0.00001)
        self.assertEqual(direction, "short-dec-long-hl")
        self.assertAlmostEqual(net, 0.00019)
        direction, net = strategy.choose_direction(-0.0003, -0.0001)
        self.assertEqual(direction, "long-dec-short-hl")
        self.assertGreater(net, 0)

    def test_seven_day_net_subtracts_fees_and_adverse_basis_only(self):
        # 0.02%/hour on $300 for 7 days, four maker legs, no gap, 5 bps buffer.
        notional = 300.0
        funding = 0.0002 * notional * 168
        fees = 4 * 0.00015 * notional
        adverse = strategy.adverse_basis_usd("short-dec-long-hl", 100, 100, 3, 5)
        self.assertAlmostEqual(adverse, 0.15)
        edge = strategy.expected_usd(0.0002, notional, 0.00015, 4, adverse)
        self.assertAlmostEqual(edge, funding - fees - 0.15)
        self.assertGreater(edge, 1.0)
        # Selling the cheap venue is a cost. A favorable gap is not credited.
        costly = strategy.adverse_basis_usd("short-dec-long-hl", 99, 100, 3, 0)
        self.assertAlmostEqual(costly, 3.0)
        gift = strategy.adverse_basis_usd("short-dec-long-hl", 101, 100, 3, 0)
        self.assertEqual(gift, 0.0)

    def test_decibel_hourly_matches_hedge_when_period_is_one_hour(self):
        rate = strategy.dec_funding_hourly({"funding_rate_bps": 1.25, "is_funding_positive": True, "funding_period_s": 3600})
        self.assertAlmostEqual(rate, 1.25 / 1e4)
        scaled = strategy.dec_funding_hourly({"funding_rate_bps": 10, "is_funding_positive": False, "funding_period_s": 8 * 3600})
        self.assertAlmostEqual(scaled, -0.001 * (3600 / 28800))


class EntryExit(unittest.TestCase):
    def test_enters_when_average_and_current_clear_the_threshold(self):
        history = hours("ETH", 6, 0.0002, 0.0)
        result = strategy.decide([snap()], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        enters = [a for a in result["actions"] if a["kind"] == "enter"]
        self.assertEqual(len(enters), 1)
        self.assertEqual(enters[0]["direction"], "short-dec-long-hl")
        self.assertGreater(enters[0]["size"], 0)
        self.assertLessEqual(enters[0]["size"] * 100, 300 + 1e-6)
        self.assertTrue(result["state"]["positions"] == {})
        self.assertIsNotNone(result["comment"])

    def test_one_snapshot_does_not_enter(self):
        history = hours("ETH", 1, 0.0002, 0.0)
        result = strategy.decide([snap()], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["kind"] == "enter" for a in result["actions"]))

    def test_current_hour_flip_blocks_entry(self):
        history = hours("ETH", 5, 0.0002, 0.0) + hours("ETH", 1, -0.0001, 0.0, start=5)
        market = snap(dec_funding_hr=-0.0001, hl_funding_hr=0.0)
        result = strategy.decide([market], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["kind"] == "enter" for a in result["actions"]))

    def test_low_open_interest_and_oi_fraction(self):
        history = hours("ETH", 6, 0.0002, 0.0)
        thin = snap(dec_oi=10)  # $1000 at $100, under the $10k floor
        result = strategy.decide([thin], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["kind"] == "enter" for a in result["actions"]))
        # 10% of 20 ETH is 2 ETH = $200, so the OI fraction binds under the $300 pair cap.
        capped = snap(dec_oi=20, hl_oi=5000)
        # oi usd = 2000, still under min. Raise mins by using a price that clears $10k
        # while the coin supply is small: price 1000, oi 20 -> $20k, 10% = $2000, pair cap $300.
        wide = snap(dec_oi=20, hl_oi=20, dec_mid=1000, hl_mid=1000, dec_mark=1000, hl_mark=1000, lot=0.001)
        result = strategy.decide([wide], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        enters = [a for a in result["actions"] if a["kind"] == "enter"]
        self.assertEqual(len(enters), 1)
        self.assertLessEqual(enters[0]["size"], 0.1 * 20 + 1e-9)
        size = strategy.entry_size(
            100, lot=0.001, min_size=0.001, coin="ETH", dec_oi=2, cfg=cfg(),
            dec_equity=141, hl_free=220, hl_mtm=331,
            existing_dec_notional=0, existing_hl_notional=0, existing_total=0,
        )
        self.assertAlmostEqual(size, 0.2)

    def test_kill_switch_blocks_entry_not_liquidation_exit(self):
        history = hours("ETH", 6, 0.0002, 0.0)
        held = snap(dec_sz=-1.0, hl_sz=1.0, dec_liq=90, dec_mark=100, hl_liq=None)
        result = strategy.decide([held], account(paused=True), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["kind"] == "enter" for a in result["actions"]))
        self.assertTrue(any(a["kind"] == "exit" and a["reason"] == "liq" for a in result["actions"]))

    def test_slot_and_total_notional_caps(self):
        history = hours("ETH", 6, 0.0002, 0.0)
        held = []
        for i, name in enumerate(("BTC", "SOL", "DOGE")):
            held.append(snap(coin=name, dec_sz=-0.01, hl_sz=0.01, dec_funding_hr=0, hl_funding_hr=0))
        eth = snap()
        result = strategy.decide(held + [eth], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["kind"] == "enter" for a in result["actions"]))
        self.assertTrue(any(a["kind"] == "signal" and a["reason"] == "capacity" for a in result["actions"]))

    def test_leverage_budget_blocks_a_new_pair(self):
        history = hours("ETH", 6, 0.0002, 0.0)
        # $400 already open on a $141 Decibel book (3x = $423) leaves too little room.
        held = snap(coin="BTC", dec_sz=-4.0, hl_sz=4.0, dec_mid=100, hl_mid=100, dec_mark=100, hl_mark=100)
        result = strategy.decide([held, snap()], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["kind"] == "enter" for a in result["actions"]))

    def test_decay_and_flip_exits_need_history(self):
        held = snap(coin="BTC", dec_sz=-0.005, hl_sz=0.005, dec_funding_hr=-0.0001, hl_funding_hr=0.0001,
                    dec_mid=63000, hl_mid=63000, dec_mark=63000, hl_mark=63000, lot=0.00001, min_size=0.00002)
        short = strategy.decide([held], account(), cfg(), hours("BTC", 3, -0.0001, 0.0001), {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["reason"] in ("decay", "flip") for a in short["actions"]))
        # First two hours pay the short-dec hedge; the last four do not. The average stays positive,
        # so this is a flip exit rather than a decay exit.
        mixed = hours("BTC", 2, 0.001, 0.0) + hours("BTC", 4, -0.00001, 0.0, start=2)
        result = strategy.decide([held], account(), cfg(), mixed, {"positions": {}, "alert_keys": {}}, NOW)
        reasons = [a["reason"] for a in result["actions"] if a["kind"] == "exit"]
        self.assertTrue("decay" in reasons or "flip" in reasons)
        three = hours("BTC", 3, 0.001, 0.0) + hours("BTC", 3, -0.00001, 0.0, start=3)
        early = strategy.decide([held], account(), cfg(), three, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["reason"] == "flip" for a in early["actions"]))
        self.assertFalse(any(a["reason"] == "decay" for a in early["actions"]))

    def test_decay_when_forward_funding_does_not_cover_close_fees(self):
        held = snap(coin="BTC", dec_sz=-0.005, hl_sz=0.005, dec_funding_hr=0.0, hl_funding_hr=0.0,
                    dec_mid=63000, hl_mid=63000, dec_mark=63000, hl_mark=63000, lot=0.00001, min_size=0.00002)
        history = hours("BTC", 6, 0.0, 0.0)
        result = strategy.decide([held], account(), cfg(), history, {"positions": {}, "alert_keys": {}}, NOW)
        self.assertTrue(any(a["reason"] == "decay" for a in result["actions"]))

    def test_liquidation_floor_is_25_percent(self):
        held = snap(coin="BTC", dec_sz=-0.005, hl_sz=0.005, dec_mark=100, hl_mark=100, dec_liq=80, hl_liq=None,
                    dec_mid=100, hl_mid=100)
        result = strategy.decide([held], account(), cfg(), [], {"positions": {}, "alert_keys": {}}, NOW)
        self.assertTrue(any(a["reason"] == "liq" for a in result["actions"]))
        safe = snap(coin="BTC", dec_sz=-0.005, hl_sz=0.005, dec_mark=100, hl_mark=100, dec_liq=70, hl_liq=130,
                    dec_mid=100, hl_mid=100, dec_funding_hr=0.001, hl_funding_hr=0)
        # 30% away is outside the floor. Funding history is empty so no decay exit.
        result = strategy.decide([safe], account(), cfg(), [], {"positions": {}, "alert_keys": {}}, NOW)
        self.assertFalse(any(a["reason"] == "liq" for a in result["actions"]))

    def test_daily_loss_flattens_and_blocks_entries(self):
        held = snap(coin="BTC", dec_sz=-0.005, hl_sz=0.005, dec_funding_hr=0.001, hl_funding_hr=0)
        other = snap()
        result = strategy.decide(
            [held, other], account(daily_loss=True), cfg(), hours("ETH", 6, 0.0002, 0.0),
            {"positions": {}, "alert_keys": {}}, NOW,
        )
        self.assertTrue(any(a["coin"] == "BTC" and a["reason"] == "daily-loss" for a in result["actions"]))
        self.assertFalse(any(a["kind"] == "enter" for a in result["actions"]))

    def test_adopts_live_hedge_and_drops_it_when_flat(self):
        held = snap(coin="BTC", dec_sz=-0.005, hl_sz=0.005, dec_funding_hr=0.00001, hl_funding_hr=0.0,
                    dec_mid=63000, hl_mid=63000, dec_mark=63000, hl_mark=63000, lot=0.00001, min_size=0.00002)
        result = strategy.decide([held], account(), cfg(), [], {"positions": {}, "alert_keys": {}}, NOW)
        pos = result["state"]["positions"]["BTC"]
        self.assertTrue(pos["adopted"])
        self.assertEqual(pos["direction"], "short-dec-long-hl")
        flat = strategy.decide([snap(coin="BTC", dec_sz=0, hl_sz=0)], account(), cfg(), [], result["state"], NOW)
        self.assertNotIn("BTC", flat["state"]["positions"])

    def test_naked_leg_is_a_repair_not_an_entry(self):
        naked = snap(coin="BTC", dec_sz=-0.005, hl_sz=0.0, dec_mid=63000, hl_mid=63000,
                     dec_mark=63000, hl_mark=63000, lot=0.00001, min_size=0.00002)
        result = strategy.decide([naked], account(), cfg(), [], {"positions": {}, "alert_keys": {}}, NOW)
        repairs = [a for a in result["actions"] if a["kind"] == "repair"]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["how"], "hedge-hl")
        dust = snap(coin="DOGE", dec_sz=1, hl_sz=0, dec_mid=0.1, hl_mid=0.1, dec_mark=0.1, hl_mark=0.1, lot=1, min_size=1)
        # $0.10 gap cannot be an HL order. Flatten.
        result = strategy.decide([dust], account(), cfg(), [], {"positions": {}, "alert_keys": {}}, NOW)
        self.assertEqual(result["actions"][0]["how"], "close")
        same = snap(coin="BTC", dec_sz=0.005, hl_sz=0.005, dec_mid=63000, hl_mid=63000, dec_mark=63000, hl_mark=63000)
        result = strategy.decide([same], account(), cfg(), [], {"positions": {}, "alert_keys": {}}, NOW)
        self.assertEqual(result["actions"][0]["how"], "close")

    def test_signal_is_quiet_the_second_hour(self):
        history = hours("ETH", 6, 0.0002, 0.0)
        state = {"positions": {}, "alert_keys": {}}
        first = strategy.decide([snap()], account(paused=True), cfg(), history, state, NOW)
        self.assertIn("SIGNAL", first["comment"])
        second = strategy.decide([snap()], account(paused=True), cfg(), history, first["state"], NOW)
        self.assertIsNone(second["comment"])

    def test_roll_day_does_not_trip_on_the_baseline_print(self):
        loss, day, start = strategy.roll_day({}, 472, "2026-11-19", 25)
        self.assertFalse(loss)
        self.assertEqual(start, 472)
        loss, _, _ = strategy.roll_day({"day": day, "day_start_equity": start}, 440, "2026-11-19", 25)
        self.assertTrue(loss)
        loss, day2, start2 = strategy.roll_day({"day": day, "day_start_equity": start}, 440, "2026-11-20", 25)
        self.assertFalse(loss)
        self.assertEqual(day2, "2026-11-20")
        self.assertEqual(start2, 440)


class Guards(unittest.TestCase):
    def test_config_defaults(self):
        c = cfg()
        self.assertEqual(c["hl_master"], "0x8bDaF4d7e8A35364A14fA93461bBACb4454ff018")
        self.assertEqual(c["decibel_subaccount"], "0xe91f3109b1558db1c77c09fa19e1f6765c2872235446b6302b11ca363231911f")
        self.assertEqual(c["risk"]["max_pair_notional_usd"], 300)
        self.assertEqual(c["risk"]["max_total_notional_usd"], 600)
        self.assertEqual(c["risk"]["max_leverage"], 3)
        self.assertEqual(c["risk"]["max_concurrent_pairs"], 3)
        self.assertEqual(c["risk"]["liq_distance_floor"], 0.25)
        self.assertEqual(c["risk"]["daily_loss_stop_usd"], 25)
        self.assertEqual(c["entry"]["min_7d_net_usd"], 1.0)
        self.assertEqual(c["entry"]["min_snapshots"], 6)
        self.assertEqual(c["entry"]["max_position_oi_fraction"], 0.1)
        self.assertEqual(c["exit"]["flip_hours"], 4)
        self.assertEqual(c["max_size_btc"], 0.01)

    def test_paper_is_the_default(self):
        old = os.environ.pop("LIVE_TRADING", None)
        try:
            self.assertTrue(strategy.paper_mode())
            os.environ["LIVE_TRADING"] = "true"
            self.assertFalse(strategy.paper_mode())
        finally:
            if old is None:
                os.environ.pop("LIVE_TRADING", None)
            else:
                os.environ["LIVE_TRADING"] = old

    def test_sanitize_strips_secrets_not_sizes(self):
        os.environ["HL_AGENT_PRIVATE_KEY"] = "super-secret-value"
        try:
            text = sanitize("key super-secret-value size 0.005")
            self.assertNotIn("super-secret-value", text)
            self.assertIn("0.005", text)
        finally:
            del os.environ["HL_AGENT_PRIVATE_KEY"]

    def test_workflows_cannot_schedule_open_or_close(self):
        with open(os.path.join(ROOT, ".github/workflows/hedge_monitor.yml")) as fh:
            monitor = fh.read()
        with open(os.path.join(ROOT, ".github/workflows/hedge_open.yml")) as fh:
            opener = fh.read()
        with open(os.path.join(ROOT, ".github/workflows/hedge_close.yml")) as fh:
            closer = fh.read()
        self.assertIn("cron:", monitor)
        self.assertIn("workflow_dispatch:", monitor)
        self.assertIn("hedge-bot", monitor)
        self.assertIn("cancel-in-progress: false", monitor)
        self.assertIn("issues: write", monitor)
        self.assertIn("contents: write", monitor)
        self.assertIn("secrets.DECIBEL_NODE_API_KEY", monitor)
        self.assertIn("secrets.DECIBEL_GAS_STATION_API_KEY", monitor)
        self.assertIn("secrets.DECIBEL_PRIVATE_KEY", monitor)
        self.assertIn("secrets.HYPERLIQUID_API_PRIVATE_KEY", monitor)
        self.assertNotIn("secrets.GEOMI_CLIENT_KEY", monitor)
        self.assertNotIn("secrets.HL_AGENT_PRIVATE_KEY", monitor)
        self.assertIn("vars.LIVE_TRADING", monitor)
        self.assertIn("vars.BOT_ENABLED", monitor)
        for text in (opener, closer):
            self.assertNotIn("schedule:", text)
            self.assertIn("workflow_dispatch:", text)
            self.assertIn("hedge-bot", text)
            self.assertIn("issues: write", text)
        self.assertIn("--live", opener)
        self.assertIn("default: true", opener)
        for text in (monitor, opener, closer):
            self.assertNotIn("actions/checkout@v", text)
            self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", text)

    def test_hl_target_matches_a_flat_open_and_a_hedged_close(self):
        # Importing the order script pulls the Hyperliquid SDK. Skip if it is not installed.
        try:
            import hedge
        except Exception as exc:
            self.skipTest(f"hedge import unavailable: {exc}")
        self.assertAlmostEqual(hedge._hl_target("open", -0.005, 0.0, 0.0), 0.005)
        self.assertAlmostEqual(hedge._hl_target("close", -0.003, -0.005, 0.005), 0.003)
        self.assertAlmostEqual(hedge._hl_target("close", 0.0, 0.0, 0.005), 0.0)


if __name__ == "__main__":
    unittest.main()
