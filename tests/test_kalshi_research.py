"""Offline safety and compatibility tests for the read-only Kalshi research facade."""
import unittest
from unittest.mock import Mock

from bot.kalshi_research import KalshiResearch


class TestKalshiResearch(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.market.return_value = {
            "ticker": "KXTEST-1", "series_ticker": "KXTEST",
            "rules_primary": "Settlement follows published source",
            "yes_bid": 45, "yes_ask_dollars": "0.47",
        }
        self.client.series.return_value = {
            "settlement_sources": [{"name": "Official"}],
            "contract_terms_url": "https://example.org/rules.pdf",
        }
        self.research = KalshiResearch(self.client)

    def test_market_prices_normalize_cents_and_dollars(self):
        result = self.research.market("KXTEST-1")
        self.assertEqual(result["yes_bid_dollars"], 0.45)
        self.assertEqual(result["yes_ask_dollars"], 0.47)
        self.assertIsNone(result["no_bid_dollars"])

    def test_rules_reuse_existing_client_and_preserve_provenance(self):
        result = self.research.rules("KXTEST-1")
        self.assertTrue(result["complete"])
        self.assertEqual(result["settlement_sources"], [{"name": "Official"}])
        self.assertEqual(result["contract_terms_url"], "https://example.org/rules.pdf")
        self.client.series.assert_called_once_with("KXTEST")

    def test_missing_series_is_incomplete_and_never_guessed(self):
        self.client.market.return_value = {"ticker": "KXTEST-1"}
        result = self.research.rules("KXTEST-1")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["settlement_sources"])
        self.client.series.assert_not_called()

    def test_invalid_tickers_do_not_hit_network(self):
        for ticker in ("", "../orders", "KX?x", "bad/ticker", " "):
            with self.assertRaises(ValueError):
                self.research.market(ticker)
        self.client.market.assert_not_called()

    def test_read_only_surface_excludes_order_and_portfolio_mutations(self):
        for name in ("place_limit_buy", "place_limit_sell", "place_resting_buy",
                     "cancel", "balance", "positions", "order", "_req"):
            self.assertFalse(hasattr(self.research, name), name)

    def test_orderbook_and_trade_limits(self):
        self.research.orderbook("KXTEST-1", depth=20)
        self.client.orderbook.assert_called_once_with("KXTEST-1", depth=20)
        self.research.trades("KXTEST-1", limit=50)
        self.client.trades.assert_called_once_with("KXTEST-1", limit=50)
        for depth in (0, 101):
            with self.assertRaises(ValueError):
                self.research.orderbook("KXTEST-1", depth)
        for limit in (0, 501):
            with self.assertRaises(ValueError):
                self.research.trades("KXTEST-1", limit)


if __name__ == "__main__":
    unittest.main()
