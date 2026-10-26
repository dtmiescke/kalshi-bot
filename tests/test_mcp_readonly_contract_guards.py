"""Regression guardrails for the existing read-only contract-rules adapter.

Inspired by third-party Kalshi MCP research. No external MCP runtime or order
route is installed by these tests.
"""
import unittest
from unittest.mock import Mock, patch

from bot.contract_pdf import (
    MAX_PDF_BYTES, is_trusted_contract_url, extract_contract_pdf_text,
    load_series_contract_pdf,
)


class Store:
    def __init__(self):
        self.data = {}

    def get_kv(self, key, default=None):
        return self.data.get(key, default)

    def set_kv(self, key, value):
        self.data[key] = value


class TestReadOnlyMCPCompatibility(unittest.TestCase):
    def test_rejects_untrusted_document_urls(self):
        for url in (
            "http://kalshi.com/rules.pdf",
            "https://kalshi.com.evil.example/rules.pdf",
            "https://evil.example/rules.pdf",
            "https://user:password@kalshi.com/rules.pdf",
            "https://kalshi.com:8443/rules.pdf",
            "file:///tmp/rules.pdf",
            "https://127.0.0.1/rules.pdf",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_trusted_contract_url(url))
        self.assertTrue(is_trusted_contract_url("https://kalshi.com/rules.pdf"))

    def test_untrusted_url_never_calls_network(self):
        get = Mock(side_effect=AssertionError("must not download"))
        self.assertIsNone(extract_contract_pdf_text("https://example.com/rules.pdf", get=get))
        get.assert_not_called()

    def test_rejects_oversized_download_before_read(self):
        response = Mock(status_code=200, headers={"Content-Length": str(MAX_PDF_BYTES + 1)})
        get = Mock(return_value=response)
        self.assertIsNone(extract_contract_pdf_text("https://kalshi.com/rules.pdf", get=get))
        self.assertFalse(response.iter_content.called)
        response.close.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_series_metadata_reuses_existing_cache_and_client(self):
        store = Store()
        url = "https://kalshi.com/rules.pdf"
        store.set_kv("contract_rules_pdf_KXNFLGAME", {
            "ts": 100, "url": url, "text": "settlement rules",
        })
        client = Mock()
        result = load_series_contract_pdf(
            client, store, "KXNFLGAME", 101,
            series_metadata={"contract_terms_url": url},
        )
        self.assertEqual(result["text"], "settlement rules")
        client.series.assert_not_called()

    def test_changed_contract_url_invalidates_cached_rules(self):
        store = Store()
        store.set_kv("contract_rules_pdf_KXNFLGAME", {
            "ts": 100, "url": "https://kalshi.com/old.pdf", "text": "old terms",
        })
        client = Mock()
        with patch("bot.contract_pdf.extract_contract_pdf_text", return_value=None) as download:
            result = load_series_contract_pdf(
                client, store, "KXNFLGAME", 101,
                series_metadata={"contract_terms_url": "https://kalshi.com/new.pdf"},
            )
        download.assert_called_once_with("https://kalshi.com/new.pdf")
        self.assertIsNone(result)
        client.series.assert_not_called()

    def test_invalid_series_ticker_never_reaches_client(self):
        client = Mock()
        self.assertIsNone(load_series_contract_pdf(client, Store(), "../orders", 100))
        client.series.assert_not_called()


if __name__ == "__main__":
    unittest.main()
