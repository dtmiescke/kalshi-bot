import unittest
from unittest.mock import Mock

from bot.contract_pdf import extract_contract_pdf_text, load_series_contract_pdf
from unittest.mock import patch


class TestContractPdf(unittest.TestCase):
    def test_rejects_non_https_and_untrusted_hosts_without_request(self):
        get = Mock()
        self.assertIsNone(extract_contract_pdf_text("http://kalshi.com/rules.pdf", get=get))
        self.assertIsNone(extract_contract_pdf_text("https://evil.example/rules.pdf", get=get))
        get.assert_not_called()

    def test_rejects_oversized_pdf(self):
        response = Mock()
        response.headers = {"Content-Length": str(9 * 1024 * 1024)}
        response.iter_content.return_value = []
        response.raise_for_status.return_value = None
        self.assertIsNone(extract_contract_pdf_text(
            "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf", get=Mock(return_value=response)))

    def test_rejects_non_pdf_content(self):
        response = Mock()
        response.headers = {}
        response.iter_content.return_value = [b"not a pdf"]
        response.raise_for_status.return_value = None
        self.assertIsNone(extract_contract_pdf_text(
            "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf", get=Mock(return_value=response)))


    def test_series_pdf_is_cached_and_not_refetched_within_seven_days(self):
        class Store:
            def __init__(self):
                self.values = {}
            def get_kv(self, key):
                return self.values.get(key)
            def set_kv(self, key, value):
                self.values[key] = value

        class Client:
            def __init__(self):
                self.calls = 0
            def series(self, ticker):
                self.calls += 1
                return {"contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf"}

        store, client = Store(), Client()
        extracted = {"url": "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf",
                     "page_count": 2, "text": "Official settlement terms", "truncated": False}
        with patch("bot.contract_pdf.extract_contract_pdf_text", return_value=extracted) as fetch:
            first = load_series_contract_pdf(client, store, "KXNFLGAME", 1000)
            second = load_series_contract_pdf(client, store, "KXNFLGAME", 1100)
        self.assertEqual(first["text"], "Official settlement terms")
        self.assertEqual(second["text"], "Official settlement terms")
        self.assertEqual(client.calls, 1)
        fetch.assert_called_once()

    def test_series_without_pdf_url_returns_unknown(self):
        class Store:
            def get_kv(self, key):
                return None
            def set_kv(self, key, value):
                raise AssertionError("must not cache absent evidence")
        class Client:
            def series(self, ticker):
                return {"ticker": ticker}
        self.assertIsNone(load_series_contract_pdf(Client(), Store(), "KXNFLGAME", 1000))


if __name__ == "__main__":
    unittest.main()
