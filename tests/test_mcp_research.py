import unittest
from unittest.mock import Mock, patch

from bot.mcp_research import market_rules_snapshot, orderbook_snapshot, recent_trades_snapshot


class TestReadOnlyMcpResearch(unittest.TestCase):
    def test_market_rules_preserves_cents_and_missing_fields(self):
        client = Mock()
        client.market.return_value = {"ticker": "KXNFL-1", "series_ticker": "KXNFL",
                                      "yes_bid": 45, "yes_ask_dollars": "0.49",
                                      "rules_primary": "Official settlement rules"}
        result = market_rules_snapshot(client, "KXNFL-1", now=123)
        self.assertEqual(result["yes_bid_dollars"], 0.45)
        self.assertEqual(result["yes_ask_dollars"], 0.49)
        self.assertIsNone(result["no_bid_dollars"])
        self.assertEqual(result["rules_primary"], "Official settlement rules")
        self.assertEqual(result["observed_at"], 123)
        client.orderbook.assert_not_called()

    def test_pdf_is_opt_in_and_uses_existing_loader(self):
        client = Mock()
        client.market.return_value = {"series_ticker": "KXNFL"}
        with patch("bot.mcp_research.load_series_contract_pdf", return_value={"text": "terms"}) as load:
            self.assertNotIn("contract_pdf", market_rules_snapshot(client, "TICKER", now=100))
            result = market_rules_snapshot(client, "TICKER", store=Mock(), now=100, include_pdf=True)
            self.assertEqual(result["contract_pdf"]["text"], "terms")
            load.assert_called_once()

    def test_invalid_ticker_rejected_before_network(self):
        client = Mock()
        for ticker in ("", "../portfolio/orders", "BTC?foo=bar", "a/b", " x y "):
            with self.subTest(ticker=ticker):
                with self.assertRaises(ValueError):
                    market_rules_snapshot(client, ticker)
        client.market.assert_not_called()

    def test_orderbook_is_bounded_and_read_only(self):
        client = Mock()
        client.orderbook.return_value = {"yes": [(0.4, 10), (0.3, 5)], "no": [(0.6, 4)]}
        result = orderbook_snapshot(client, "KXNFL-1", depth=1, now=123)
        self.assertEqual(result["yes_bids"], [(0.4, 10)])
        client.orderbook.assert_called_once_with("KXNFL-1", depth=1)
        with self.assertRaises(ValueError):
            orderbook_snapshot(client, "KXNFL-1", depth=21)
        client.place_resting_buy.assert_not_called()

    def test_trades_limit(self):
        client = Mock()
        client.trades.return_value = [{"id": 1}, {"id": 2}]
        result = recent_trades_snapshot(client, "KXNFL-1", limit=1, now=123)
        self.assertEqual(len(result["trades"]), 1)
        client.trades.assert_called_once_with("KXNFL-1", limit=1)
        with self.assertRaises(ValueError):
            recent_trades_snapshot(client, "KXNFL-1", limit=501)


if __name__ == "__main__":
    unittest.main()
