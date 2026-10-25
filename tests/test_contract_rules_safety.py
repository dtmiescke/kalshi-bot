"""Regression tests for official rules evidence cache and read-only URL validation."""
import unittest
from unittest.mock import Mock, patch

from bot.contract_pdf import extract_contract_pdf_text, load_series_contract_pdf


class Store:
    def __init__(self):
        self.data = {}

    def get_kv(self, key):
        return self.data.get(key)

    def set_kv(self, key, value):
        self.data[key] = value


class RulesSafetyTests(unittest.TestCase):
    def test_changed_rules_url_refreshes_cache(self):
        store = Store()
        client = Mock()
        first_url = "https://kalshi-public-docs.s3.amazonaws.com/first.pdf"
        second_url = "https://kalshi-public-docs.s3.amazonaws.com/updated.pdf"

        def extract(url):
            return {"url": url, "text": url, "page_count": 1, "truncated": False}

        with patch("bot.contract_pdf.extract_contract_pdf_text", side_effect=extract) as fetch:
            first = load_series_contract_pdf(client, store, "KXNFLGAME", 100, {"contract_terms_url": first_url})
            second = load_series_contract_pdf(client, store, "KXNFLGAME", 101, {"contract_terms_url": second_url})
            repeated = load_series_contract_pdf(client, store, "KXNFLGAME", 102, {"contract_terms_url": second_url})
        self.assertEqual(first["url"], first_url)
        self.assertEqual(second["url"], second_url)
        self.assertEqual(repeated["url"], second_url)
        self.assertEqual(fetch.call_count, 2)

    def test_cached_rules_not_used_if_current_metadata_is_untrusted(self):
        store = Store()
        store.set_kv("contract_rules_pdf_KXNFLGAME", {
            "ts": 100, "url": "https://kalshi.com/old.pdf", "text": "old"})
        result = load_series_contract_pdf(
            Mock(), store, "KXNFLGAME", 101,
            {"contract_terms_url": "https://attacker.example/new.pdf"})
        self.assertIsNone(result)

    def test_invalid_ticker_does_not_access_store_or_client(self):
        store = Mock()
        client = Mock()
        for ticker in ("../KXNFLGAME", "NFL?other", "A\\nB", "x" * 81):
            self.assertIsNone(load_series_contract_pdf(client, store, ticker, 100))
        store.get_kv.assert_not_called()
        client.series.assert_not_called()

    def test_redirect_not_accepted_as_rules(self):
        response = Mock()
        response.status_code = 302
        response.headers = {"Location": "https://kalshi.com/other.pdf"}
        get = Mock(return_value=response)
        self.assertIsNone(extract_contract_pdf_text("https://kalshi.com/rules.pdf", get=get))
        response.iter_content.assert_not_called()
        response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
