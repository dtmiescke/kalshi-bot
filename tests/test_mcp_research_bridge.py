import unittest
from unittest.mock import Mock

from bot.mcp_research_bridge import ResearchBridge, ALLOWED_TOOLS


class ResearchBridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.bridge = ResearchBridge(self.client)

    def test_explicit_read_only_allowlist(self):
        self.assertEqual(ALLOWED_TOOLS, {
            "market_details", "series_rules", "orderbook_snapshot",
        })
        for tool in ("place_order", "cancel", "balance", "positions", "__dict__"):
            with self.assertRaises(ValueError):
                self.bridge.call(tool, {"ticker": "KXNFLGAME"})
        self.client.assert_not_called()

    def test_series_uses_existing_client(self):
        self.client.series.return_value = {
            "settlement_sources": [{"name": "Official source"}],
            "rules_primary": "Team wins",
        }
        result = self.bridge.call("series_rules", {"ticker": "KXNFLGAME"})
        self.client.series.assert_called_once_with("KXNFLGAME")
        self.assertEqual(result["rules_primary"], "Team wins")
        self.assertEqual(result["source"], "kalshi_series")

    def test_market_prices_normalize_cents_and_dollars(self):
        self.client.market.return_value = {
            "yes_bid": 46, "yes_ask_dollars": "0.49",
            "no_bid": 51, "no_ask_dollars": "0.54",
        }
        result = self.bridge.call("market_details", {"ticker": "ABC-123"})
        self.assertEqual(result["yes_bid_dollars"], 0.46)
        self.assertEqual(result["yes_ask_dollars"], 0.49)
        self.assertEqual(result["no_bid_dollars"], 0.51)

    def test_orderbook_reuses_client_and_bounds_depth(self):
        self.client.orderbook.return_value = {"yes": [(0.5, 10)], "no": []}
        result = self.bridge.call("orderbook_snapshot", {"ticker": "ABC", "depth": 5})
        self.client.orderbook.assert_called_once_with("ABC", depth=5)
        self.assertEqual(result["bids"]["yes"], [(0.5, 10)])
        for depth in (0, 21, True, "10", 1.5):
            with self.assertRaises(ValueError):
                self.bridge.call("orderbook_snapshot", {"ticker": "ABC", "depth": depth})

    def test_rejects_untrusted_tickers_and_extra_arguments(self):
        for ticker in ("../portfolio/orders", "ABC?x=y", "ABC/DEF", "", "A" * 129):
            with self.assertRaises(ValueError):
                self.bridge.call("market_details", {"ticker": ticker})
        with self.assertRaises(ValueError):
            self.bridge.call("market_details", {"ticker": "ABC", "method": "POST"})
        self.client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
