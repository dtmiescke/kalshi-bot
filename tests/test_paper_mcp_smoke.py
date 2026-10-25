"""Offline paper-mode smoke test and unique read-only research endpoint contracts.

All exchange calls are mocked; this suite cannot submit an exchange order.
"""
import os
import unittest
from unittest.mock import Mock, patch

from bot.config import Settings
from bot.kalshi_client import KalshiClient
from bot.mcp_readonly import research_tool
from bot.risk import asks_from_book, fill_plan


class PaperSmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = KalshiClient("https://example.invalid/trade-api/v2")
        self.client._req = Mock(side_effect=self._read_only_exchange)
        self.calls = []

    def _read_only_exchange(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("paper smoke attempted an exchange write")
        if path == "/markets/TEST":
            return {"market": {"ticker": "TEST", "series_ticker": "KXNFLGAME",
                               "rules_primary": "Winner", "settlement_source": "Official"}}
        if path == "/series/KXNFLGAME":
            return {"series": {"ticker": "KXNFLGAME"}}
        if path == "/markets/TEST/orderbook":
            return {"orderbook": {"yes": [[40, 10]], "no": [[40, 10]]}}
        return {"live_data": None}

    def test_default_paper_research_and_simulated_fill_never_write(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
            self.assertEqual(settings.mode, "paper")
            self.assertFalse(settings.live_confirmed)
            self.assertFalse(settings.combos_enabled)
            self.assertEqual(settings.selected_series(), {
                "KXNFLGAME", "KXNCAAFGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME"
            })
            settings.validate()
        rules = research_tool(self.client, "rules", "TEST")
        self.assertTrue(rules["available"])
        book = research_tool(self.client, "orderbook", "TEST")
        asks = asks_from_book(book)["yes"]
        contracts, avg_price = fill_plan(asks, max_dollars=5, limit_price=0.61)
        self.assertGreater(contracts, 0)
        self.assertAlmostEqual(avg_price, 0.60)
        self.assertTrue(self.calls)
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_official_live_evidence_is_read_only_and_missing_is_unknown(self):
        self.assertEqual(self.client.sports_filters(), {"live_data": None})
        self.assertEqual(self.client.milestone_live_data("game-123"), {"live_data": None})
        self.assertEqual(self.client.milestone_game_stats("game-123"), {"live_data": None})
        self.assertEqual(self.client.event_live_data("KXBTC-26"), {"live_data": None})
        self.assertEqual([path for _, path in self.calls], [
            "/search/filters_by_sport", "/live_data/milestone/game-123",
            "/live_data/milestone/game-123/game_stats", "/live_data/events/KXBTC-26"
        ])
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_untrusted_identifier_rejected_without_network(self):
        for value in ("", "../orders", "a/b", "x?query=1", None):
            for method in (self.client.milestone_live_data,
                           self.client.milestone_game_stats, self.client.event_live_data):
                with self.subTest(value=value, method=method.__name__):
                    with self.assertRaises(ValueError):
                        method(value)
        self.client._req.assert_not_called()

    def test_mcp_order_tools_rejected_before_any_exchange_request(self):
        for tool in ("place_order", "cancel", "portfolio", "trade", "execute"):
            with self.subTest(tool=tool):
                with self.assertRaises(ValueError):
                    research_tool(self.client, tool, "TEST")
        self.client._req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
