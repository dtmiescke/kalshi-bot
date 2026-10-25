"""No-network tests: MCP research must not create a second execution path."""
import unittest
from unittest.mock import Mock

from bot.mcp_readonly import research_tool


class ReadOnlyResearchTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.market.return_value = {
            "ticker": "KXNFLGAME-EXAMPLE",
            "series_ticker": "KXNFLGAME",
            "rules_primary": "Official final result determines settlement",
        }
        self.client.series.return_value = {
            "settlement_sources": "Official league results",
        }
        self.client.orderbook.return_value = {"yes": [(0.51, 10)], "no": [(0.48, 5)]}

    def test_existing_client_reused_for_market(self):
        self.assertEqual(research_tool(self.client, "market", "KXNFLGAME-EXAMPLE")["ticker"],
                         "KXNFLGAME-EXAMPLE")
        self.client.market.assert_called_once_with("KXNFLGAME-EXAMPLE")

    def test_existing_orderbook_normalization_reused(self):
        self.assertEqual(research_tool(self.client, "orderbook", "KXNFLGAME-EXAMPLE")["yes"],
                         [(0.51, 10)])
        self.client.orderbook.assert_called_once()

    def test_rules_use_existing_market_and_series(self):
        result = research_tool(self.client, "rules", "KXNFLGAME-EXAMPLE")
        self.assertTrue(result["available"])
        self.client.series.assert_called_once_with("KXNFLGAME")

    def test_no_order_write_or_dynamic_method_dispatch(self):
        for name in ("place_limit_buy", "cancel", "positions", "balance",
                     "place_resting_buy", "__getattribute__", "trades"):
            with self.assertRaises(ValueError):
                research_tool(self.client, name, "KXNFLGAME")
        self.client.place_limit_buy.assert_not_called()
        self.client.cancel.assert_not_called()

    def test_invalid_tickers_fail_before_api_calls(self):
        for ticker in ("", "../portfolio/orders", "KX?cursor=x", "KX/other", None, "a" * 81):
            with self.assertRaises(ValueError):
                research_tool(self.client, "market", ticker)
        self.client.market.assert_not_called()

    def test_missing_series_ticker_never_guesses(self):
        self.client.market.return_value = {"rules_primary": "A published outcome"}
        research_tool(self.client, "rules", "KXTEST")
        self.client.series.assert_not_called()


if __name__ == "__main__":
    unittest.main()
