import unittest
from unittest.mock import Mock

from bot.research_bridge import READ_ONLY_RESEARCH_TOOLS, research_tool


class ResearchBridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.market.return_value = {"yes_bid": 42, "yes_ask_dollars": "0.45"}
        self.client.series.return_value = {"ticker": "KXNFL", "rules_primary": "Official"}
        self.client.orderbook.return_value = {"yes": [(0.4, 5)], "no": []}

    def test_market_price_units(self):
        result = research_tool(self.client, "market_snapshot", "KXNFL-TEST")
        self.assertEqual(result["yes_bid_dollars"], 0.42)
        self.assertEqual(result["yes_ask_dollars"], 0.45)
        self.client.market.assert_called_once_with("KXNFL-TEST")

    def test_series_rules_are_not_claimed_verified(self):
        result = research_tool(self.client, "series_rules", "KXNFL")
        self.assertFalse(result["settlement_verified"])
        self.client.series.assert_called_once_with("KXNFL")

    def test_orderbook_uses_existing_client(self):
        result = research_tool(self.client, "orderbook_snapshot", "KXNFL")
        self.assertEqual(result["orderbook"]["yes"], [(0.4, 5)])
        self.client.orderbook.assert_called_once_with("KXNFL", depth=20)

    def test_rejects_unknown_and_write_tools(self):
        for tool in ("place_order", "cancel", "portfolio", "_req", "balance"):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                research_tool(self.client, tool, "KXNFL")
        self.client.assert_not_called()

    def test_rejects_unsafe_tickers(self):
        for ticker in ("../portfolio", "A?cursor=x", "", "a/b", "a b", None):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                research_tool(self.client, "market_snapshot", ticker)
        self.client.assert_not_called()

    def test_explicit_allowlist(self):
        self.assertEqual(set(READ_ONLY_RESEARCH_TOOLS),
                         {"market_snapshot", "series_rules", "orderbook_snapshot"})


if __name__ == "__main__":
    unittest.main()
