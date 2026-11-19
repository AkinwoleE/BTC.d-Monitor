"""The funding hedge must be invisible to the other bots only when attributed."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hedge_book

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SUB = "0xe91f3109b1558db1c77c09fa19e1f6765c2872235446b6302b11ca363231911f"
MASTER = "0x8bDaF4d7e8A35364A14fA93461bBACb4454ff018"


def _read(path):
    with open(os.path.join(ROOT, path)) as handle:
        return handle.read()


def fallback_rows():
    rows, source = hedge_book.load_book()
    assert source == "config"
    return rows


class Filter(unittest.TestCase):
    def test_fallback_only_match_still_alerts(self):
        rows = fallback_rows()
        # Static config size, no state file, no hedge- tag, no opposite leg.
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.005, rows, account=SUB, book_source="config"))
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "hyperliquid", "BTC", 0.005, rows, account=MASTER, book_source="config"))

    def test_verified_state_hedge_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            config = os.path.join(tmp, "config.json")
            with open(config, "w") as handle:
                json.dump({
                    "decibel_subaccount": SUB,
                    "hl_master": MASTER,
                    "fallback_book": [{"venue": "decibel", "coin": "BTC", "size": -0.005}],
                }, handle)
            with open(state, "w") as handle:
                json.dump({"positions": {"BTC": {"dec_sz": -0.005, "hl_sz": 0.005}}}, handle)
            rows, source = hedge_book.load_book(state, config)
            self.assertEqual(source, "state")
            self.assertTrue(hedge_book.ignore_for_other_bot(
                False, "decibel", "BTC", -0.005, rows, account=SUB, book_source=source))
            self.assertTrue(hedge_book.ignore_for_other_bot(
                False, "hyperliquid", "BTC", 0.005, rows, account=MASTER, book_source=source))
            # The bot that already recorded its own position keeps managing it.
            self.assertFalse(hedge_book.ignore_for_other_bot(
                True, "decibel", "BTC", -0.005, rows, account=SUB, book_source=source))
            # A different account is not this hedge.
            self.assertFalse(hedge_book.ignore_for_other_bot(
                False, "decibel", "BTC", -0.005, rows, account="0x" + "ab" * 20,
                book_source=source))

    def test_empty_state_does_not_fall_back_to_the_static_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            config = os.path.join(tmp, "config.json")
            with open(config, "w") as handle:
                json.dump({
                    "decibel_subaccount": SUB,
                    "hl_master": MASTER,
                    "fallback_book": [{"venue": "decibel", "coin": "BTC", "size": -0.005}],
                }, handle)
            with open(state, "w") as handle:
                json.dump({"positions": {}}, handle)
            rows, source = hedge_book.load_book(state, config)
            self.assertEqual(source, "state")
            self.assertEqual(rows, [])
            self.assertFalse(hedge_book.ignore_for_other_bot(
                False, "decibel", "BTC", -0.005, rows, account=SUB, book_source=source))

    def test_hedge_order_or_fill_is_ignored(self):
        rows = fallback_rows()
        orders = [{"client_order_id": "hedge-open-1", "symbol": "BTC/USD"}]
        self.assertTrue(hedge_book.order_is_hedge(orders[0]))
        self.assertTrue(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.001, rows, orders, account=SUB, book_source="config"))
        fills = [{"client_order_id": "hedge-close-9", "symbol": "BTC/USD"}]
        self.assertTrue(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.005, [], fills=fills, account=SUB, book_source="config"))
        eth_only = [{"client_order_id": "hedge-open-2", "symbol": "ETH/USD"}]
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.02, rows, eth_only, account=SUB, book_source="config"))
        other = [{"clientOrderId": "div-123", "symbol": "BTC/USD"}]
        self.assertFalse(hedge_book.order_is_hedge(other[0]))
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.02, rows, other, account=SUB, book_source="config"))

    def test_opposite_leg_is_ignored_and_unrelated_size_alerts(self):
        rows = fallback_rows()
        # Decibel short offset by a Hyperliquid long of the same size.
        self.assertTrue(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.005, rows, account=SUB,
            book_source="config", opposite_size=0.005))
        # Same sign is not a hedge.
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.005, rows, account=SUB,
            book_source="config", opposite_size=-0.005))
        # Unrelated Decibel size, even if some HL BTC position exists.
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.02, rows, account=SUB,
            book_source="config", opposite_size=0.005))
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "ETH", -3.0, rows, account=SUB, book_source="config"))

    def test_state_records_another_coin_without_hiding_btc(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            config = os.path.join(tmp, "config.json")
            with open(config, "w") as handle:
                json.dump({"decibel_subaccount": SUB, "hl_master": MASTER}, handle)
            with open(state, "w") as handle:
                json.dump({"positions": {"ETH": {"dec_sz": -1.5, "hl_sz": 1.5}}}, handle)
            rows, source = hedge_book.load_book(state, config)
            self.assertTrue(hedge_book.ignore_for_other_bot(
                False, "decibel", "ETH", -1.5, rows, account=SUB, book_source=source))
            self.assertFalse(hedge_book.ignore_for_other_bot(
                False, "decibel", "BTC", -0.005, rows, account=SUB, book_source=source))

    def test_callers_use_the_filter_and_the_prefix(self):
        div = _read("divergence_trader.py")
        btcd = _read("btcd_trader.py")
        turtle = _read("turtle_hyperliquid_trader.py")
        hedge = _read(os.path.join("bot", "hedge.py"))
        self.assertIn("_ignore_hedge_btc", div)
        self.assertIn("hedge_ignored", div)
        self.assertIn("book_source=source", div)
        self.assertIn("ignore_for_other_bot", btcd)
        self.assertIn("book_source=source", btcd)
        self.assertIn("ignore_for_other_bot", turtle)
        self.assertIn("book_source=source", turtle)
        self.assertIn("ORDER_PREFIX", hedge)
        self.assertIn('f"{ORDER_PREFIX}{mode}-', hedge)


if __name__ == "__main__":
    unittest.main()
