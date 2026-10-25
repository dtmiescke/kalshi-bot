import unittest
from unittest.mock import Mock

from bot.kalshi_research import ReadOnlyKalshiResearch


class ReadOnlyKalshiResearchTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.research = ReadOnlyKalshiResearch(self.client)

    def test_normalizes_cents_and_dollar_prices(self):
        self.client.market.return_value = {
            "title": "Test", "yes_bid": 42,
            "yes_ask_dollars": "0.45", "no_bid": 55,
        }
        result = self.research.market_snapshot("KXTEST-1")
        self.assertEqual(result["yes_bid"], 0.42)
        self.assertEqual(result["yes_ask"], 0.45)
        self.assertEqual(result["no_bid"], 0.55)
        self.assertIsNone(result["no_ask"])
        self.client.market.assert_called_once_with("KXTEST-1")

    def test_rules_reuse_existing_client(self):
        self.client.series.return_value = {"title": "Test", "settlement_sources": ["official"]}
        result = self.research.series_rules("KXTEST")
        self.assertEqual(result["settlement_sources"], ["official"])
        self.client.series.assert_called_once_with("KXTEST")

    def test_orderbook_preserves_bids_without_fabricated_asks(self):
        self.client.orderbook.return_value = {"yes": [(0.40, 2)], "no": [(0.50, 3)]}
        result = self.research.orderbook_snapshot("KXTEST", depth=3)
        self.assertEqual(result["yes_bids"], [(0.40, 2)])
        self.assertNotIn("yes_ask", result)
        self.client.orderbook.assert_called_once_with("KXTEST", depth=3)

    def test_invalid_ticker_never_calls_client(self):
        for ticker in ("", "../portfolio", "KX?test", "A/B"):
            with self.assertRaises(ValueError):
                self.research.market_snapshot(ticker)
        self.client.market.assert_not_called()

    def test_depth_is_bounded(self):
        for depth in (0, 21, -1):
            with self.assertRaises(ValueError):
                self.research.orderbook_snapshot("KXTEST", depth)
        self.client.orderbook.assert_not_called()

    def test_no_order_or_portfolio_capabilities(self):
        for name in ("place_order", "place_resting_buy", "balance", "positions", "cancel_order"):
            self.assertFalse(hasattr(self.research, name))


if __name__ == "__main__":
    unittest.main()
