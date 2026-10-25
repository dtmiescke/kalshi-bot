"""Contract evidence refresh tests: do not keep outdated rules indefinitely."""
import unittest
from unittest.mock import Mock, patch

from bot.contract_pdf import CACHE_SECONDS, load_series_contract_pdf


class Store:
    def __init__(self):
        self.data = {}

    def get_kv(self, key, default=None):
        return self.data.get(key, default)

    def set_kv(self, key, value):
        self.data[key] = value


class ContractFreshnessTests(unittest.TestCase):
    def test_same_document_is_refreshed_after_daily_ttl(self):
        store = Store()
        client = Mock()
        url = "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf"
        series = {"contract_terms_url": url}
        with patch("bot.contract_pdf.extract_contract_pdf_text", side_effect=[
            {"url": url, "text": "first version", "page_count": 1, "truncated": False},
            {"url": url, "text": "revised version", "page_count": 1, "truncated": False},
        ]) as fetch:
            first = load_series_contract_pdf(client, store, "KXNFLGAME", 1000, series)
            cached = load_series_contract_pdf(client, store, "KXNFLGAME", 1001, series)
            revised = load_series_contract_pdf(client, store, "KXNFLGAME", 1000 + CACHE_SECONDS, series)
        self.assertEqual(first["text"], "first version")
        self.assertEqual(cached["text"], "first version")
        self.assertEqual(revised["text"], "revised version")
        self.assertEqual(fetch.call_count, 2)
        client.series.assert_not_called()

    def test_document_url_change_invalidates_cache_immediately(self):
        store = Store()
        client = Mock()
        first_url = "https://kalshi-public-docs.s3.amazonaws.com/old.pdf"
        next_url = "https://kalshi-public-docs.s3.amazonaws.com/new.pdf"
        with patch("bot.contract_pdf.extract_contract_pdf_text", side_effect=[
            {"url": first_url, "text": "old", "page_count": 1, "truncated": False},
            {"url": next_url, "text": "new", "page_count": 1, "truncated": False},
        ]) as fetch:
            load_series_contract_pdf(client, store, "KXNFLGAME", 1000,
                                     {"contract_terms_url": first_url})
            updated = load_series_contract_pdf(client, store, "KXNFLGAME", 1001,
                                               {"contract_terms_url": next_url})
        self.assertEqual(updated["text"], "new")
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
