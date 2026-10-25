"""End-to-end, offline contract research safety tests for existing MCP-inspired adapters.

Use one mocked Kalshi client and one store; no network, credentials, or orders.
"""
import unittest
from unittest.mock import Mock, patch

from bot.mcp_readonly import research_tool
from bot.kalshi_research import ReadOnlyKalshiResearch
from bot.market_rules import summarize_rules


class _Store:
    def __init__(self):
        self.items = {}

    def get_kv(self, key):
        return self.items.get(key)

    def set_kv(self, key, value):
        self.items[key] = value


class McpResearchEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.market.return_value = {
            "ticker": "KXNFLGAME-TEST",
            "series_ticker": "KXNFLGAME",
            "rules_primary": "The official league final score determines the outcome.",
            "settlement_source": "Official league results",
            "yes_bid": 42,
            "yes_ask_dollars": "0.45",
        }
        self.client.series.return_value = {
            "title": "NFL game",
            "settlement_sources": "Official league results",
            "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/nfl.pdf",
        }
        self.client.orderbook.return_value = {"yes": [(0.42, 3)], "no": [(0.55, 2)]}
        self.store = _Store()

    def assert_no_order_writes(self):
        for method in ("place_resting_buy", "place_limit_buy", "cancel", "cancel_order"):
            getattr(self.client, method).assert_not_called()

    def test_shared_client_market_rules_and_book(self):
        facade = ReadOnlyKalshiResearch(self.client)
        market = facade.market_snapshot("KXNFLGAME-TEST")
        self.assertEqual(market["yes_bid"], 0.42)
        self.assertEqual(market["yes_ask"], 0.45)
        rules = research_tool(self.client, "rules", "KXNFLGAME-TEST")
        self.assertEqual(rules["status"], "terms_found")
        self.assertEqual(rules["terms"]["settlement_source"], "Official league results")
        self.assertEqual(research_tool(self.client, "orderbook", "KXNFLGAME-TEST")["yes"], [(0.42, 3)])
        self.assert_no_order_writes()

    def test_pdf_reuses_store_and_invalidates_changed_source(self):
        first_url = self.client.series.return_value["contract_terms_url"]
        second_url = "https://kalshi-public-docs.s3.amazonaws.com/nfl-revised.pdf"
        with patch("bot.contract_pdf.extract_contract_pdf_text") as extract:
            extract.side_effect = lambda url: {
                "url": url, "text": "Official contract terms", "page_count": 1, "truncated": False,
            }
            first = research_tool(self.client, "rules_pdf", "KXNFLGAME", store=self.store, now=100)
            cached = research_tool(self.client, "rules_pdf", "KXNFLGAME", store=self.store, now=101)
            self.assertEqual(first, cached)
            self.assertEqual(extract.call_count, 1)
            self.client.series.return_value["contract_terms_url"] = second_url
            revised = research_tool(self.client, "rules_pdf", "KXNFLGAME", store=self.store, now=102)
            self.assertEqual(revised["url"], second_url)
            self.assertEqual(extract.call_count, 2)
            self.assertNotEqual(first["url"], revised["url"])
            self.assertEqual(first["url"], first_url)
        self.assert_no_order_writes()

    def test_mcp_envelope_cannot_override_conflicting_official_sources(self):
        envelope = {
            "structuredContent": {
                "market": {
                    "rules_primary": "Official result",
                    "settlement_source": "Source A",
                }
            }
        }
        rules = summarize_rules(envelope, {"settlement_sources": "Source B"})
        self.assertEqual(rules["status"], "review_required")
        self.assertIn("settlement_source_conflict", rules["flags"])
        for unsafe_tool in ("place_order", "cancel_order", "balance", "positions", "trades"):
            with self.assertRaises(ValueError):
                research_tool(self.client, unsafe_tool, "KXNFLGAME-TEST")
        self.assert_no_order_writes()


if __name__ == "__main__":
    unittest.main()
