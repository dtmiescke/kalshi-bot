"""Regression tests for existing Kalshi contract PDF research integration."""
import unittest
from unittest.mock import Mock, patch

from bot.contract_pdf import load_series_contract_pdf


class ContractPdfSourceTests(unittest.TestCase):
    def test_rules_pdf_url_is_supported_without_second_client(self):
        client = Mock()
        client.series.return_value = {
            "ticker": "TESTSERIES",
            "rules_pdf_url": "https://kalshi.com/official-rules.pdf",
        }
        store = Mock()
        store.get_kv.return_value = None
        with patch("bot.contract_pdf.extract_contract_pdf_text", return_value={
            "url": "https://kalshi.com/official-rules.pdf",
            "text": "Official rules", "page_count": 1, "truncated": False,
        }) as extract:
            result = load_series_contract_pdf(client, store, "TESTSERIES", 1234)
        self.assertEqual(result["text"], "Official rules")
        extract.assert_called_once_with("https://kalshi.com/official-rules.pdf")
        client.series.assert_called_once_with("TESTSERIES")
        client.create_order.assert_not_called()
        store.set_kv.assert_called_once()

    def test_rejects_untrusted_rules_pdf_url(self):
        client = Mock()
        client.series.return_value = {
            "rules_pdf_url": "https://untrusted.example/contract.pdf",
        }
        store = Mock()
        with patch("bot.contract_pdf.extract_contract_pdf_text") as extract:
            self.assertIsNone(load_series_contract_pdf(client, store, "TESTSERIES", 1234))
        extract.assert_not_called()
        store.set_kv.assert_not_called()

    def test_cached_pdf_reused_without_network_download(self):
        url = "https://kalshi.com/official-rules.pdf"
        client = Mock()
        client.series.return_value = {"rules_pdf_url": url}
        store = Mock()
        store.get_kv.return_value = {"url": url, "text": "cached rules", "ts": 1234}
        with patch("bot.contract_pdf.extract_contract_pdf_text") as extract:
            result = load_series_contract_pdf(client, store, "TESTSERIES", 1235)
        self.assertEqual(result["text"], "cached rules")
        extract.assert_not_called()
        store.set_kv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
