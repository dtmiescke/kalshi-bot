import unittest
from bot.kalshi_research import contract_research, _safe_document_url


class StubClient:
    def __init__(self, market=None, series=None):
        self.market_data = market if market is not None else {
            "ticker": "KXTEST-1", "series_ticker": "KXTEST",
            "title": "Test", "status": "active",
            "rules_primary": "Official outcome only",
        }
        self.series_data = series if series is not None else {
            "rules": "Official series rules",
            "contract_url": "https://kalshi.com/rules/example",
        }
        self.calls = []

    def market(self, ticker):
        self.calls.append(("market", ticker))
        return self.market_data

    def series(self, ticker):
        self.calls.append(("series", ticker))
        return self.series_data


class TestReadOnlyKalshiResearch(unittest.TestCase):
    def test_uses_existing_client_and_never_trades(self):
        client = StubClient()
        result = contract_research(client, "KXTEST-1")
        self.assertEqual(client.calls, [("market", "KXTEST-1"), ("series", "KXTEST")])
        self.assertFalse(result["trade_ready"])
        self.assertFalse(result["missing_rules"])
        self.assertEqual(result["provenance"], "kalshi_api_via_existing_client")

    def test_missing_series_does_not_guess(self):
        client = StubClient(market={"title": "Ambiguous"})
        result = contract_research(client, "KXTEST-1")
        self.assertIsNone(result["series_ticker"])
        self.assertTrue(result["missing_rules"])
        self.assertEqual(len(client.calls), 1)

    def test_missing_market_fails_closed(self):
        self.assertFalse(contract_research(StubClient(market={}), "KXTEST-1")["trade_ready"])

    def test_rejects_invalid_tickers(self):
        for ticker in ("", "../portfolio/orders", "A/B", "A?x=1", "x" * 121):
            with self.assertRaises(ValueError):
                contract_research(StubClient(), ticker)

    def test_document_urls_are_untrusted(self):
        for url in ("http://example.com", "file:///tmp/a", "https://user:pw@x.test/a",
                    "javascript:alert(1)", "https://"):
            self.assertIsNone(_safe_document_url(url))
        self.assertEqual(_safe_document_url("https://kalshi.com/rules/a"),
                         "https://kalshi.com/rules/a")

    def test_no_order_write_methods_exposed(self):
        import bot.kalshi_research as research
        self.assertFalse(hasattr(research, "place_order"))
        self.assertFalse(hasattr(research, "cancel_order"))


if __name__ == "__main__":
    unittest.main()
