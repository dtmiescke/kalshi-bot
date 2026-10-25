"""Offline tests for the read-only MCP-inspired research bridge."""
import unittest
from unittest.mock import Mock

from bot.readonly_research import ReadOnlyKalshiResearch


class ReadOnlyResearchTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.bridge = ReadOnlyKalshiResearch(self.client)

    def test_rules_use_existing_client_and_preserve_sources(self):
        self.client.market.return_value = {
            "ticker": "KXTEST-1", "series_ticker": "KXTEST",
            "rules_primary": "Official yes/no settlement",
            "contract_terms_url": "https://kalshi.com/rules.pdf",
        }
        self.client.series.return_value = {
            "settlement_sources": [{"name": "official source"}],
            "prohibitions": "No prohibited activity",
        }
        data = self.bridge.get_market_rules("KXTEST-1")
        self.assertEqual(data["status"], "available")
        self.assertEqual(data["market_rules"]["rules_primary"], "Official yes/no settlement")
        self.assertEqual(data["series_rules"]["settlement_sources"][0]["name"], "official source")
        self.client.market.assert_called_once_with("KXTEST-1")
        self.client.series.assert_called_once_with("KXTEST")

    def test_snapshot_preserves_dollars_and_bid_sides(self):
        self.client.market.return_value = {
            "ticker": "KXTEST-1", "yes_bid": 41,
            "yes_ask_dollars": "0.43", "no_bid": 57,
        }
        self.client.orderbook.return_value = {"yes": [(0.41, 3)], "no": [(0.57, 2)]}
        data = self.bridge.get_market_snapshot("KXTEST-1", depth=5)
        self.assertAlmostEqual(data["yes_bid_dollars"], 0.41)
        self.assertAlmostEqual(data["yes_ask_dollars"], 0.43)
        self.assertAlmostEqual(data["no_bid_dollars"], 0.57)
        self.assertIsNone(data["no_ask_dollars"])
        self.client.orderbook.assert_called_once_with("KXTEST-1", depth=5)

    def test_invalid_inputs_do_not_call_client(self):
        for ticker in ("", "../portfolio/orders", "X?foo=1", "X/Y", "a" * 201):
            with self.assertRaises(ValueError):
                self.bridge.get_market_rules(ticker)
        for depth in (0, 101, True, 1.5):
            with self.assertRaises(ValueError):
                self.bridge.get_market_snapshot("KXTEST-1", depth=depth)
        for limit in (0, 501, True):
            with self.assertRaises(ValueError):
                self.bridge.get_recent_trades("KXTEST-1", limit=limit)
        self.client.assert_not_called()

    def test_no_order_write_tools_exposed(self):
        public = {name for name in dir(self.bridge) if not name.startswith("_")}
        self.assertEqual(public, {
            "get_market_rules", "get_market_snapshot",
            "get_market", "get_series", "get_recent_trades",
        })

    def test_missing_market_is_unknown_not_fabricated(self):
        self.client.market.return_value = {}
        self.assertEqual(self.bridge.get_market_rules("KXTEST-1")["status"], "unavailable")
        self.client.series.assert_not_called()


if __name__ == "__main__":
    unittest.main()
