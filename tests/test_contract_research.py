"""Offline regression tests for the read-only contract research adapter."""
import unittest
from unittest.mock import Mock

from bot.contract_research import contract_research, _safe_document_url


class ContractResearchTests(unittest.TestCase):
    def test_uses_existing_client_and_preserves_provenance(self):
        client = Mock()
        client.market.return_value = {
            "ticker": "TEST-YES", "series_ticker": "TEST", "status": "active",
            "rules_primary": "Official primary rule", "settlement_source": "Official feed",
            "rules_pdf_url": "https://kalshi.com/terms.pdf",
        }
        client.series.return_value = {"ticker": "TEST", "restrictions": "Restricted"}
        result = contract_research(client, "TEST-YES")
        self.assertTrue(result["complete"])
        self.assertEqual(result["rules"]["primary"], "Official primary rule")
        self.assertEqual(result["document_urls"], ["https://kalshi.com/terms.pdf"])
        client.market.assert_called_once_with("TEST-YES")
        client.series.assert_called_once_with("TEST")
        self.assertFalse(hasattr(client, "create_order"))

    def test_missing_rules_fail_closed(self):
        client = Mock()
        client.market.return_value = {"ticker": "TEST", "series_ticker": "SERIES"}
        client.series.return_value = {"ticker": "SERIES"}
        result = contract_research(client, "TEST")
        self.assertFalse(result["complete"])
        self.assertIn("Incomplete", result["warning"])

    def test_market_mismatch_rejected(self):
        client = Mock()
        client.market.return_value = {"ticker": "OTHER"}
        with self.assertRaises(ValueError):
            contract_research(client, "TEST")
        client.series.assert_not_called()

    def test_series_mismatch_rejected(self):
        client = Mock()
        client.market.return_value = {"ticker": "TEST", "series_ticker": "SERIES"}
        client.series.return_value = {"ticker": "OTHER"}
        with self.assertRaises(ValueError):
            contract_research(client, "TEST")

    def test_unsafe_urls_rejected(self):
        for value in ("http://example.com/a", "file:///tmp/x", "https://u:p@example.com/a", "javascript:alert(1)"):
            self.assertIsNone(_safe_document_url(value))
        self.assertEqual(_safe_document_url("https://example.com/rules"), "https://example.com/rules")

    def test_rejects_unsafe_ticker(self):
        with self.assertRaises(ValueError):
            contract_research(Mock(), "../portfolio")


if __name__ == "__main__":
    unittest.main()
