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


class TestConfirmedIOCResponse(unittest.TestCase):
    def test_accepts_explicit_zero_and_partial_fills_with_order_id(self):
        from bot.engine import Engine
        self.assertEqual(Engine._confirmed_ioc_fill(
            {"order_id": "ord-1", "fill_count_fp": "0"}, 5), ("ord-1", 0))
        self.assertEqual(Engine._confirmed_ioc_fill(
            {"id": "ord-2", "fill_count": 3}, 5), ("ord-2", 3))

    def test_rejects_missing_order_id_or_fill_count(self):
        from bot.engine import Engine
        for order in ({"fill_count_fp": "0"}, {"order_id": "ord-1"},
                      {"order_id": "ord-2", "fill_count_fp": "nan"},
                      {"order_id": "ord-3", "fill_count_fp": "6"}):
            with self.subTest(order=order):
                with self.assertRaises(RuntimeError):
                    Engine._confirmed_ioc_fill(order, 5)
