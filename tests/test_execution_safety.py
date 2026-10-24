"""Regression tests for price normalization and bounded order-book sizing.

These tests are offline and never submit orders.
"""
import unittest

from bot.kalshi_client import num, price
from bot.risk import asks_from_book, fill_plan, fill_plan_contracts, taker_fee


class TestExecutionMathSafety(unittest.TestCase):
    def test_dollar_price_fields_take_precedence_over_legacy_cents(self):
        self.assertEqual(price({"yes_bid_dollars": "0.42", "yes_bid": 41}, "yes_bid"), 0.42)
        self.assertEqual(price({"yes_bid": 42}, "yes_bid"), 0.42)
        self.assertIsNone(price({}, "yes_bid"))

    def test_fill_counts_prefer_fixed_point_fields(self):
        self.assertEqual(num({"fill_count_fp": "2.5", "fill_count": 2}, "fill_count"), 2.5)
        self.assertEqual(num({}, "fill_count"), 0.0)

    def test_buy_asks_are_mirrored_from_opposite_side_bids(self):
        asks = asks_from_book({"yes": [(0.60, 4)], "no": [(0.35, 3)]})
        self.assertEqual(asks["yes"], [(0.65, 3)])
        self.assertEqual(asks["no"], [(0.40, 4)])

    def test_fill_plan_never_exceeds_limit_or_budget(self):
        contracts, avg = fill_plan([(0.40, 3), (0.42, 5), (0.50, 20)], 2.05, 0.42)
        self.assertEqual(contracts, 5)
        self.assertAlmostEqual(avg, 0.408)
        self.assertLessEqual(contracts * avg, 2.05)
        self.assertLessEqual(avg, 0.42)

    def test_exact_contract_sizing_respects_available_depth(self):
        contracts, avg = fill_plan_contracts([(0.40, 2), (0.41, 3)], 8, 0.41)
        self.assertEqual(contracts, 5)
        self.assertAlmostEqual(avg, 0.406)

    def test_fee_rounds_up_to_cent(self):
        self.assertEqual(taker_fee(1, 0.50, 0.07), 0.02)
        self.assertEqual(taker_fee(10, 0.50, 0.07), 0.18)


if __name__ == "__main__":
    unittest.main()


class TestArbitrageUnwindPause(unittest.TestCase):
    def test_incomplete_unwind_persists_reconciliation_pause(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from bot.engine import Engine

        class Store:
            def __init__(self):
                self.rows = []
                self.kv = {}
            def add_bet(self, row):
                self.rows.append(row)
            def set_kv(self, key, value):
                self.kv[key] = value

        class Data:
            def orderbook(self, ticker):
                return {"yes": [(0.50, 10)], "no": []}

        class Trade:
            def place_limit_sell(self, ticker, side, contracts, bid):
                return {"fill_count_fp": "2"}

        engine = Engine.__new__(Engine)
        engine.s = SimpleNamespace(mode="demo", taker_fee_rate=0.07)
        engine.store = Store()
        engine.data = Data()
        engine.trade = Trade()
        engine.betting_ok = True
        with patch("bot.engine.alerts.send"):
            residual = engine._unwind_leg(
                "TICKER", "yes", 0.50, "buy-1", 5, "EVENT", "ARB"
            )
        self.assertEqual(residual, 3)
        self.assertEqual(engine.store.rows[0]["contracts"], 3)
        self.assertFalse(engine.betting_ok)
        self.assertIn("execution_reconciliation_required", engine.store.kv)
