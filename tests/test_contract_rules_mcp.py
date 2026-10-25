"""Read-only contract-rules adapter: no second order execution path."""
import unittest
from unittest.mock import Mock

from bot.kalshi_client import KalshiClient


class ContractRulesTests(unittest.TestCase):
    def test_collects_rules_from_existing_client(self):
        client = KalshiClient("https://api.elections.kalshi.com/trade-api/v2")
        client.market = Mock(return_value={
            "series_ticker": "KXTEST", "rules_primary": "Official result",
            "rules_secondary": "Void if postponed", "early_close_condition": "None",
        })
        client.series = Mock(return_value={
            "settlement_source": "Official league", "restrictions": "No restrictions",
        })
        result = client.market_rules("KXTEST-26")
        self.assertEqual(result["rules_primary"], "Official result")
        self.assertEqual(result["settlement_source"], "Official league")
        self.assertTrue(result["complete"])
        client.market.assert_called_once_with("KXTEST-26")
        client.series.assert_called_once_with("KXTEST")

    def test_incomplete_rules_never_marked_complete(self):
        client = KalshiClient("https://api.elections.kalshi.com/trade-api/v2")
        client.market = Mock(return_value={"series_ticker": "KXTEST"})
        client.series = Mock(return_value={})
        result = client.market_rules("KXTEST-26")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["rules_primary"])

    def test_invalid_tickers_rejected_before_network(self):
        client = KalshiClient("https://api.elections.kalshi.com/trade-api/v2")
        client.market = Mock()
        for ticker in ("", "../portfolio/orders", "a?b", "a/b"):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                client.market_rules(ticker)
        client.market.assert_not_called()

    def test_no_authenticated_order_calls(self):
        client = KalshiClient("https://api.elections.kalshi.com/trade-api/v2")
        calls = []
        def fake_req(method, path, params=None, body=None, retries=3):
            calls.append((method, path))
            if path.startswith("/markets/"):
                return {"market": {"rules_primary": "Official", "series_ticker": "KXTEST"}}
            return {"series": {"settlement_source": "Official league"}}
        client._req = fake_req
        client.market_rules("KXTEST-26")
        self.assertEqual(calls, [
            ("GET", "/markets/KXTEST-26"),
            ("GET", "/series/KXTEST"),
        ])


if __name__ == "__main__":
    unittest.main()
