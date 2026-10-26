"""Regression tests for external MCP-style quote normalization.

No external MCP process or second order path is needed: the existing Kalshi
client owns the canonical quote conversion.
"""
import unittest

from bot.kalshi_client import price


class TestQuoteNormalization(unittest.TestCase):
    def test_legacy_cents_and_dollar_strings(self):
        self.assertEqual(price({"yes_bid": 57}, "yes_bid"), 0.57)
        self.assertEqual(price({"yes_bid_dollars": "0.5700"}, "yes_bid"), 0.57)
        self.assertEqual(price({"yes_bid": 57, "yes_bid_dollars": "0.57"}, "yes_bid"), 0.57)
        self.assertEqual(price({"yes_bid": 57, "yes_bid_dollars": "0.575"}, "yes_bid"), 0.575)

    def test_missing_is_unknown(self):
        self.assertIsNone(price({}, "yes_bid"))
        self.assertIsNone(price(None, "yes_bid"))

    def test_bad_data_fails_closed(self):
        for bad in ("NaN", "Infinity", "-Infinity", "-0.01", "1.01", "oops", True):
            with self.subTest(bad=bad):
                self.assertIsNone(price({"yes_bid_dollars": bad}, "yes_bid"))
        for bad in ("NaN", "Infinity", "-1", "101", "oops", True):
            with self.subTest(cents=bad):
                self.assertIsNone(price({"yes_bid": bad}, "yes_bid"))

    def test_contradictory_fields_are_not_trusted(self):
        self.assertIsNone(price({"yes_bid": 80, "yes_bid_dollars": "0.20"}, "yes_bid"))
        self.assertIsNone(price({"yes_bid": "oops", "yes_bid_dollars": "0.20"}, "yes_bid"))
        self.assertIsNone(price({"yes_bid": 20, "yes_bid_dollars": "NaN"}, "yes_bid"))


if __name__ == "__main__":
    unittest.main()
