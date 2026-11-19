"""The funding hedge must be invisible to the other bots on these accounts."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hedge_book

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(path):
    with open(os.path.join(ROOT, path)) as handle:
        return handle.read()
SUB = "0xe91f3109b1558db1c77c09fa19e1f6765c2872235446b6302b11ca363231911f"
MASTER = "0x8bDaF4d7e8A35364A14fA93461bBACb4454ff018"


def book():
    rows, source = hedge_book.load_book()
    assert source == "config"
    return rows


class Filter(unittest.TestCase):
    def test_fallback_book_matches_the_open_hedge(self):
        rows = book()
        self.assertTrue(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.005, rows, account=SUB))
        self.assertTrue(hedge_book.ignore_for_other_bot(
            False, "hyperliquid", "BTC", "0.005", rows, account=MASTER))
        # A different size is someone else's trade.
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.02, rows, account=SUB))
        # Another account is not this hedge, even at the same size.
        self.assertFalse(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.005, rows, account="0x" + "ab" * 20))
        # The bot that already recorded the position keeps managing it.
        self.assertFalse(hedge_book.ignore_for_other_bot(
            True, "decibel", "BTC", -0.005, rows, account=SUB))

    def test_client_order_prefix_ignores_a_resting_hedge_order(self):
        rows = book()
        orders = [
            {"client_order_id": "hedge-open-1", "symbol": "BTC/USD", "size": 0.001},
            {"clientOrderId": "div-123", "symbol": "ETH/USD"},
        ]
        self.assertTrue(hedge_book.order_is_hedge(orders[0]))
        self.assertFalse(hedge_book.order_is_hedge(orders[1]))
        # Size does not match the book; the tag is enough for this market.
        self.assertTrue(hedge_book.ignore_for_other_bot(
            False, "decibel", "BTC", -0.001, rows, orders, account=SUB))
        # An ETH hedge order does not hide a BTC position.
        eth_only = [{"client_order_id": "hedge-open-2", "symbol": "ETH/USD"}]
        self.assertFalse(hedge_book.position_is_hedge(
            "decibel", "BTC", -0.02, rows, eth_only, account=SUB))

    def test_state_file_replaces_the_fallback(self):
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
                False, "decibel", "BTC", -0.005, rows, account=SUB))
            with open(state, "w") as handle:
                json.dump({"positions": {"ETH": {"dec_sz": -1.5, "hl_sz": 1.5}}}, handle)
            rows, source = hedge_book.load_book(state, config)
            self.assertTrue(hedge_book.ignore_for_other_bot(
                False, "decibel", "ETH", -1.5, rows, account=SUB))
            self.assertFalse(hedge_book.ignore_for_other_bot(
                False, "decibel", "BTC", -0.005, rows, account=SUB))

    def test_callers_use_the_filter_and_the_prefix(self):
        div = _read("divergence_trader.py")
        btcd = _read("btcd_trader.py")
        turtle = _read("turtle_hyperliquid_trader.py")
        hedge = _read(os.path.join("bot", "hedge.py"))
        self.assertIn("_ignore_hedge_btc", div)
        self.assertIn("hedge_ignored", div)
        self.assertIn("ignore_for_other_bot", btcd)
        self.assertIn("ignore_for_other_bot", turtle)
        self.assertIn("ORDER_PREFIX", hedge)
        self.assertIn('f"{ORDER_PREFIX}{mode}-', hedge)


if __name__ == "__main__":
    unittest.main()
