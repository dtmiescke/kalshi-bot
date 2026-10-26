"""Integration-boundary tests for the existing read-only Kalshi MCP-inspired research.

No live credentials, network calls, order placement or UI dependencies.
"""
import unittest
from unittest.mock import patch

from bot.contract_research import contract_research
from bot.contract_pdf import is_trusted_contract_url, load_series_contract_pdf


class Store:
    def __init__(self):
        self.values = {}

    def get_kv(self, key, default=None):
        return self.values.get(key, default)

    def set_kv(self, key, value):
        self.values[key] = value


class Client:
    def __init__(self):
        self.market_calls = []
        self.series_calls = []

    def market(self, ticker):
        self.market_calls.append(ticker)
        return {
            "ticker": ticker, "series_ticker": "KXNFLGAME",
            "rules_primary": "Team wins", "settlement_source": "Official result",
            "rules_pdf_url": "https://kalshi.com/rules.pdf",
        }

    def series(self, ticker):
        self.series_calls.append(ticker)
        return {"ticker": ticker, "contract_terms_url": "https://kalshi.com/rules.pdf"}


class ContractResearchBoundaryTests(unittest.TestCase):
    def test_uses_existing_client_and_deduplicates_documents(self):
        client = Client()
        result = contract_research(client, "KXNFLGAME-TEST")
        self.assertEqual(client.market_calls, ["KXNFLGAME-TEST"])
        self.assertEqual(client.series_calls, ["KXNFLGAME"])
        self.assertEqual(result["document_urls"], ["https://kalshi.com/rules.pdf"])
        self.assertTrue(result["complete"])

    def test_invalid_ticker_never_calls_client(self):
        client = Client()
        for ticker in ("", "../market", "foo?x=1", "foo/bar"):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                contract_research(client, ticker)
        self.assertEqual(client.market_calls, [])

    def test_market_ticker_mismatch_fails_closed(self):
        client = Client()
        client.market = lambda ticker: {"ticker": "OTHER", "series_ticker": "KXNFLGAME"}
        with self.assertRaises(ValueError):
            contract_research(client, "EXPECTED")

    def test_missing_settlement_source_is_explicitly_incomplete(self):
        client = Client()
        client.market = lambda ticker: {"ticker": ticker, "rules_primary": "Yes if X"}
        result = contract_research(client, "TEST")
        self.assertFalse(result["complete"])
        self.assertIn("Incomplete", result["warning"])


class ContractPDFBoundaryTests(unittest.TestCase):
    def test_rejects_untrusted_urls(self):
        for url in (
            "http://kalshi.com/rules.pdf",
            "https://kalshi.com.evil.example/rules.pdf",
            "https://user:pass@kalshi.com/rules.pdf",
            "https://kalshi.com:8443/rules.pdf",
            "file:///tmp/rules.pdf",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_trusted_contract_url(url))

    def test_cache_reused_then_invalidated_by_changed_url(self):
        store = Store()
        client = Client()
        first = {"url": "https://kalshi.com/rules.pdf", "text": "official rules"}
        with patch("bot.contract_pdf.extract_contract_pdf_text", return_value=first) as extract:
            a = load_series_contract_pdf(client, store, "KXNFLGAME", 1000)
            b = load_series_contract_pdf(client, store, "KXNFLGAME", 1001)
            self.assertEqual(a, b)
            self.assertEqual(extract.call_count, 1)
            updated = {"ticker": "KXNFLGAME", "rules_pdf_url": "https://kalshi.com/new.pdf"}
            c = load_series_contract_pdf(client, store, "KXNFLGAME", 1002, updated)
            self.assertEqual(c["url"], first["url"])  # mocked extraction provenance
            self.assertEqual(extract.call_count, 2)

    def test_bad_series_never_fetches(self):
        client, store = Client(), Store()
        with patch("bot.contract_pdf.extract_contract_pdf_text") as extract:
            self.assertIsNone(load_series_contract_pdf(client, store, "../bad", 1000))
            extract.assert_not_called()
            self.assertEqual(client.series_calls, [])


if __name__ == "__main__":
    unittest.main()
