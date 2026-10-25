"""No network, no orders: regression tests for the MCP-style research bridge."""
import unittest
from unittest.mock import patch

from bot.mcp_research_bridge import TOOLS, research_tool


class FakeClient:
    def __init__(self):
        self.calls = []

    def market(self, ticker):
        self.calls.append(("market", ticker))
        return {"ticker": ticker, "title": "Test", "secret": "omit"}

    def series(self, ticker):
        self.calls.append(("series", ticker))
        return {"ticker": ticker, "contract_terms_url": "https://kalshi.com/terms.pdf"}

    def orderbook(self, ticker, depth=10):
        self.calls.append(("orderbook", ticker, depth))
        return {"yes": [(0.45, 3)], "no": [(0.53, 4)]}

    def place_limit_buy(self, *args, **kwargs):
        raise AssertionError("orders are forbidden")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()

    def test_only_three_read_tools(self):
        self.assertEqual(set(TOOLS), {
            "kalshi_market_research", "kalshi_series_rules", "kalshi_orderbook_research"})

    def test_market_filters_fields(self):
        out = research_tool(self.client, None, "kalshi_market_research",
                            {"ticker": "KXTEST"}, 123)
        self.assertEqual(out["market"], {"ticker": "KXTEST", "title": "Test"})
        self.assertEqual(self.client.calls, [("market", "KXTEST")])

    @patch("bot.mcp_research_bridge.load_series_contract_pdf")
    def test_rules_use_existing_pdf_loader(self, loader):
        loader.return_value = {"text": "Official rules", "url": "https://kalshi.com/terms.pdf"}
        out = research_tool(self.client, object(), "kalshi_series_rules",
                            {"ticker": "KXTEST"}, 123)
        self.assertEqual(out["contract_pdf"]["text"], "Official rules")
        loader.assert_called_once()

    def test_orderbook_is_bids_and_dollars(self):
        out = research_tool(self.client, None, "kalshi_orderbook_research",
                            {"ticker": "KXTEST", "depth": 5}, 123)
        self.assertEqual(out["book_type"], "bids_only")
        self.assertEqual(out["book"]["yes"], [[0.45, 3.0]])

    def test_fail_closed_on_invalid_tool_ticker_depth(self):
        for name, args in [
            ("kalshi_place_order", {"ticker": "KXTEST"}),
            ("kalshi_market_research", {"ticker": "../portfolio/orders"}),
            ("kalshi_orderbook_research", {"ticker": "KXTEST", "depth": 0}),
            ("kalshi_orderbook_research", {"ticker": "KXTEST", "depth": True}),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                research_tool(self.client, None, name, args, 123)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
