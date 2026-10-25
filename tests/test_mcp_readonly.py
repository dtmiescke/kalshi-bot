import unittest
from unittest.mock import Mock, patch

from bot.mcp_readonly import KalshiResearchTools


class TestKalshiResearchTools(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.tools = KalshiResearchTools(self.client)

    def test_market_normalizes_cents_and_dollars(self):
        self.client.market.return_value = {
            "title": "Game", "yes_bid": 45, "yes_ask_dollars": "0.47",
            "no_bid": 53, "no_ask_dollars": "0.55",
        }
        result = self.tools.market_snapshot("KXNFLGAME")
        self.assertEqual(result["yes_bid"], 0.45)
        self.assertEqual(result["yes_ask"], 0.47)
        self.assertEqual(result["no_bid"], 0.53)
        self.assertEqual(result["no_ask"], 0.55)

    def test_invalid_ticker_never_reaches_client(self):
        for ticker in ("../portfolio/orders", "A/B", "A?x=1", ""):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                self.tools.market_snapshot(ticker)
        self.client.market.assert_not_called()

    def test_depth_is_bounded(self):
        for depth in (0, 21, -1):
            with self.subTest(depth=depth), self.assertRaises(ValueError):
                self.tools.orderbook_snapshot("ABC", depth)
        self.client.orderbook.assert_not_called()

    def test_uses_existing_orderbook(self):
        self.client.orderbook.return_value = {"yes": [(0.4, 5)], "no": []}
        result = self.tools.orderbook_snapshot("ABC", 5)
        self.assertEqual(result["bids"]["yes"], [(0.4, 5)])
        self.client.orderbook.assert_called_once_with("ABC", depth=5)

    def test_series_rules_are_read_only(self):
        self.client.series.return_value = {"title": "NFL", "contract_terms_url": "https://kalshi.com/a.pdf"}
        result = self.tools.series_rules("KXNFL")
        self.assertEqual(result["title"], "NFL")
        self.client.series.assert_called_once_with("KXNFL")

    def test_pdf_uses_existing_extractor(self):
        with patch("bot.contract_pdf.load_series_contract_pdf", return_value={"text": "rules"}) as loader:
            result = self.tools.contract_pdf_evidence("KXNFL", object(), 100)
        self.assertEqual(result["text"], "rules")
        loader.assert_called_once()

    def test_no_order_mutation_methods(self):
        for name in ("place_limit_buy", "place_limit_sell", "place_resting_buy", "cancel", "order", "balance", "positions"):
            self.assertFalse(hasattr(self.tools, name))


if __name__ == "__main__":
    unittest.main()
