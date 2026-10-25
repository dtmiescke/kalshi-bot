import unittest
from unittest.mock import Mock, patch

from bot.contract_pdf import extract_contract_pdf_text, load_series_contract_pdf


class TestContractPdf(unittest.TestCase):
    def test_rejects_non_https_and_untrusted_hosts_without_request(self):
        get = Mock()
        urls = (
            "http://kalshi.com/rules.pdf",
            "https://evil.example/rules.pdf",
            "https://kalshi.com.evil.example/rules.pdf",
            "https://user@kalshi.com/rules.pdf",
            "https://kalshi.com:8443/rules.pdf",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(extract_contract_pdf_text(url, get=get))
        get.assert_not_called()

    def test_rejects_oversized_pdf(self):
        response = Mock()
        response.headers = {"Content-Length": str(9 * 1024 * 1024)}
        response.iter_content.return_value = []
        response.raise_for_status.return_value = None
        get = Mock(return_value=response)
        self.assertIsNone(extract_contract_pdf_text(
            "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf", get=get))
        get.assert_called_once()
        response.close.assert_called_once()

    def test_rejects_non_pdf_content(self):
        response = Mock()
        response.headers = {}
        response.iter_content.return_value = [b"not a pdf"]
        response.raise_for_status.return_value = None
        self.assertIsNone(extract_contract_pdf_text(
            "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf",
            get=Mock(return_value=response)))

    def test_rejects_stream_that_exceeds_size_limit(self):
        response = Mock()
        response.headers = {}
        response.iter_content.return_value = [b"x" * (8 * 1024 * 1024 + 1)]
        response.raise_for_status.return_value = None
        self.assertIsNone(extract_contract_pdf_text(
            "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf",
            get=Mock(return_value=response)))

    def test_cache_avoids_repeat_pdf_fetch(self):
        class Store:
            def __init__(self):
                self.values = {}
            def get_kv(self, key):
                return self.values.get(key)
            def set_kv(self, key, value):
                self.values[key] = value
        store = Store()
        client = Mock()
        series = {"contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf"}
        extracted = {"url": series["contract_terms_url"], "page_count": 1,
                     "text": "Official terms", "truncated": False}
        with patch("bot.contract_pdf.extract_contract_pdf_text", return_value=extracted) as fetch:
            first = load_series_contract_pdf(client, store, "KXNFLGAME", 1000, series)
            second = load_series_contract_pdf(client, store, "KXNFLGAME", 1100, series)
        self.assertEqual(first["text"], "Official terms")
        self.assertEqual(second["text"], "Official terms")
        fetch.assert_called_once()
        client.series.assert_not_called()

    def test_missing_or_untrusted_series_url_is_unknown(self):
        class Store:
            def get_kv(self, key):
                return None
            def set_kv(self, key, value):
                raise AssertionError("must not cache absent evidence")
        self.assertIsNone(load_series_contract_pdf(Mock(), Store(), "KXNFLGAME", 1000, {}))
        self.assertIsNone(load_series_contract_pdf(
            Mock(), Store(), "KXNFLGAME", 1000, {"contract_url": "https://evil.example/a.pdf"}))


if __name__ == "__main__":
    unittest.main()
