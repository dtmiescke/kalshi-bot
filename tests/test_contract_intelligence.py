"""Regression tests for read-only contract research (no network or orders)."""
import unittest
from unittest.mock import Mock

from bot.contract_intelligence import contract_context


class TestContractIntelligence(unittest.TestCase):
    def test_reads_existing_client_only(self):
        client = Mock()
        client.market.return_value = {
            "ticker": "KXTEST-1", "series_ticker": "KXTEST",
            "title": "Will it happen?", "rules_primary": "Official market rule",
            "status": "open",
        }
        client.series.return_value = {
            "settlement_sources": [{"name": "Official source"}],
            "contract_url": "https://example.org/rules.pdf",
        }
        out = contract_context(client, "KXTEST-1")
        client.market.assert_called_once_with("KXTEST-1")
        client.series.assert_called_once_with("KXTEST")
        client.place_limit_buy.assert_not_called()
        client.place_resting_buy.assert_not_called()
        self.assertEqual(out["rules_primary"], "Official market rule")
        self.assertFalse(out["rules_pdf_verified"])
        self.assertEqual(out["confidence"], "official_metadata_only")

    def test_missing_series_is_unknown_not_inferred(self):
        client = Mock()
        client.market.return_value = {"title": "A", "event_ticker": "ABC-123"}
        out = contract_context(client, "ABC-123-A")
        self.assertIsNone(out["series_ticker"])
        self.assertIsNone(out["settlement_sources"])
        client.series.assert_not_called()

    def test_rejects_invalid_ticker_without_requests(self):
        client = Mock()
        for ticker in ("", "../portfolio/orders", "ABC?x=1", None):
            with self.assertRaises(ValueError):
                contract_context(client, ticker)
        client.market.assert_not_called()

    def test_missing_market_fails_closed(self):
        client = Mock()
        client.market.return_value = {}
        with self.assertRaises(ValueError):
            contract_context(client, "KXTEST-1")
        client.series.assert_not_called()

    def test_bad_series_ticker_fails_closed(self):
        client = Mock()
        client.market.return_value = {"series_ticker": "../portfolio"}
        with self.assertRaises(ValueError):
            contract_context(client, "KXTEST-1")
        client.series.assert_not_called()


if __name__ == "__main__":
    unittest.main()
