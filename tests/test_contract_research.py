import unittest
from unittest.mock import Mock

from bot.contract_research import contract_rules


class TestContractResearch(unittest.TestCase):
    def test_reuses_client_and_never_places_orders(self):
        client = Mock()
        client.market.return_value = {
            "series_ticker": "KXTEST", "rules_primary": "Official text",
            "status": "active", "rules_pdf_url": "https://example.com/terms.pdf",
        }
        client.series.return_value = {"settlement_source": "Official source"}
        result = contract_rules(client, "KXTEST-26")
        self.assertEqual(result["market"]["rules_primary"], "Official text")
        self.assertEqual(result["series"]["settlement_source"], "Official source")
        self.assertFalse(result["trade_eligible"])
        client.place_limit_buy.assert_not_called()
        client.place_resting_buy.assert_not_called()
        client.market.assert_called_once_with("KXTEST-26")

    def test_rejects_bad_ticker_before_request(self):
        client = Mock()
        for ticker in ("../portfolio", "ABC?foo", "", "A/B"):
            with self.assertRaises(ValueError):
                contract_rules(client, ticker)
        client.market.assert_not_called()

    def test_missing_fields_are_not_invented(self):
        client = Mock()
        client.market.return_value = {"series_ticker": "TEST", "rules_pdf_url": "http://unsafe/terms"}
        client.series.side_effect = RuntimeError("offline")
        result = contract_rules(client, "TEST")
        self.assertEqual(result["market"], {})
        self.assertEqual(result["series"], {})
        self.assertEqual(result["source_links"], {})
        self.assertFalse(result["trade_eligible"])

    def test_unavailable_market_fails_closed(self):
        client = Mock()
        client.market.return_value = {}
        self.assertEqual(contract_rules(client, "TEST")["status"], "unavailable")
        client.series.assert_not_called()


if __name__ == "__main__":
    unittest.main()
