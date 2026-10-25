import unittest
from unittest.mock import Mock, patch

from bot.mcp_research import KalshiResearchTools


class TestKalshiResearchTools(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.tools = KalshiResearchTools(self.client)

    def test_delegates_to_existing_client(self):
        self.tools.get_market("KXNFLGAME-123")
        self.client.market.assert_called_once_with("KXNFLGAME-123")
        self.tools.get_orderbook("KXNFLGAME-123", depth=7)
        self.client.orderbook.assert_called_once_with("KXNFLGAME-123", depth=7)
        self.tools.get_trades("KXNFLGAME-123", limit=9)
        self.client.trades.assert_called_once_with("KXNFLGAME-123", limit=9)
        self.tools.get_sports_events("KXNFLGAME")
        self.client.sports_events.assert_called_once_with("KXNFLGAME")

    def test_rejects_path_injection_and_unbounded_reads(self):
        for ticker in ("", "../portfolio", "A?x=y", "A/B", "x\nheader"):
            with self.subTest(ticker=ticker):
                with self.assertRaises(ValueError):
                    self.tools.get_market(ticker)
        for depth in (0, 101):
            with self.assertRaises(ValueError):
                self.tools.get_orderbook("KXNFLGAME", depth)
        for limit in (0, 501):
            with self.assertRaises(ValueError):
                self.tools.get_trades("KXNFLGAME", limit)
        self.client.assert_not_called()

    def test_no_order_or_portfolio_capabilities(self):
        for method in ("place_limit_buy", "place_limit_sell", "cancel",
                       "place_resting_buy", "balance", "positions", "order"):
            self.assertFalse(hasattr(self.tools, method), method)

    def test_contract_pdf_uses_existing_extractor(self):
        store = Mock()
        series = {"contract_terms_url": "https://kalshi.com/rules.pdf"}
        self.client.series.return_value = series
        tools = KalshiResearchTools(self.client, store)
        with patch("bot.mcp_research.load_series_contract_pdf", return_value={"text": "rules"}) as loader:
            result = tools.get_contract_terms("KXNFLGAME", now=1000)
        self.assertEqual(result["contract_pdf"]["text"], "rules")
        loader.assert_called_once_with(self.client, store, "KXNFLGAME", 1000, series)

    def test_missing_store_does_not_fetch_pdf(self):
        self.client.series.return_value = {"ticker": "KXNFLGAME"}
        result = self.tools.get_contract_terms("KXNFLGAME")
        self.assertIsNone(result["contract_pdf"])


if __name__ == "__main__":
    unittest.main()
