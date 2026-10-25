import unittest
from unittest.mock import Mock, patch

from bot.kalshi_research_tools import KalshiResearchTools


class TestKalshiResearchTools(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.tools = KalshiResearchTools(self.client)

    def test_reuses_existing_client_for_market_data(self):
        self.tools.market("KXBTC15M-TEST")
        self.tools.orderbook("KXBTC15M-TEST", depth=5)
        self.tools.recent_trades("KXBTC15M-TEST", limit=25)
        self.tools.sports_events("KXNFLGAME")
        self.client.market.assert_called_once_with("KXBTC15M-TEST")
        self.client.orderbook.assert_called_once_with("KXBTC15M-TEST", depth=5)
        self.client.trades.assert_called_once_with("KXBTC15M-TEST", limit=25)
        self.client.sports_events.assert_called_once_with("KXNFLGAME")

    def test_invalid_inputs_never_reach_client(self):
        for bad in ("../portfolio/orders", "A?x=1", "", "A/B"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.tools.market(bad)
        for bad in (0, 101):
            with self.assertRaises(ValueError):
                self.tools.orderbook("ABC", depth=bad)
        for bad in (0, 501):
            with self.assertRaises(ValueError):
                self.tools.recent_trades("ABC", limit=bad)
        self.client.assert_not_called()

    def test_rules_use_existing_pdf_reader_and_store(self):
        store = Mock()
        client = Mock()
        client.series.return_value = {"contract_terms_url": "https://kalshi.com/rules.pdf"}
        tools = KalshiResearchTools(client, store)
        with patch("bot.kalshi_research_tools.load_series_contract_pdf",
                   return_value={"text": "terms"}) as pdf:
            result = tools.contract_rules("KXNFLGAME", now=123)
        self.assertEqual(result["pdf"]["text"], "terms")
        client.series.assert_called_once_with("KXNFLGAME")
        pdf.assert_called_once_with(client, store, "KXNFLGAME", 123,
                                    series_metadata=client.series.return_value)

    def test_no_order_methods_exposed(self):
        for name in ("place_limit_buy", "place_limit_sell", "cancel",
                     "place_resting_buy", "create_order", "amend_order"):
            self.assertFalse(hasattr(self.tools, name))


if __name__ == "__main__":
    unittest.main()
