"""Tests for additive Kalshi documented API capabilities (no network calls)."""
import unittest
from unittest.mock import Mock, patch

from bot.kalshi_client import KalshiClient


class TestKalshiBulkOrderbooks(unittest.TestCase):
    def test_bulk_books_normalize_prices_and_sort_levels(self):
        client = KalshiClient("https://example.com/trade-api/v2")
        client._req = Mock(return_value={"orderbooks": [{
            "ticker": "KXTEST",
            "orderbook_fp": {
                "yes_dollars": [["0.40", "2"], ["0.60", "4"]],
                "no_dollars": [["0.20", "8"]],
            },
        }]})
        result = client.market_orderbooks(["KXTEST", "KXTEST"])
        self.assertEqual(result["KXTEST"]["yes"], [(0.6, 4.0), (0.4, 2.0)])
        self.assertEqual(result["KXTEST"]["no"], [(0.2, 8.0)])
        client._req.assert_called_once_with("GET", "/markets/orderbooks", params={"tickers": ["KXTEST"]})

    def test_bulk_books_reject_bad_input_before_request(self):
        client = KalshiClient("https://example.com/trade-api/v2")
        client._req = Mock()
        for tickers in ([], ["BAD/TICKER"], ["KX1"] * 101):
            with self.subTest(tickers=len(tickers)):
                with self.assertRaises(ValueError):
                    client.market_orderbooks(tickers)
        client._req.assert_not_called()


class TestKalshiOrderV2(unittest.TestCase):
    def setUp(self):
        self.client = KalshiClient("https://example.com/trade-api/v2")
        self.client._req = Mock(return_value={"order_id": "simulated"})

    def test_v2_uses_documented_path_and_payload(self):
        result = self.client.place_order_v2(
            "KXTEST", "bid", 3, 0.42, time_in_force="immediate_or_cancel"
        )
        self.assertEqual(result, {"order_id": "simulated"})
        method, path = self.client._req.call_args.args
        body = self.client._req.call_args.kwargs["body"]
        self.assertEqual((method, path), ("POST", "/portfolio/events/orders"))
        self.assertEqual(body["side"], "bid")
        self.assertEqual(body["count"], "3.00")
        self.assertEqual(body["price"], "0.4200")
        self.assertEqual(body["time_in_force"], "immediate_or_cancel")
        self.assertTrue(body["client_order_id"])
        self.assertEqual(body["self_trade_prevention_type"], "taker_at_cross")

    def test_v2_invalid_inputs_never_send_request(self):
        invalid = [
            ("BAD/TICKER", "bid", 1, 0.50),
            ("KXTEST", "yes", 1, 0.50),
            ("KXTEST", "bid", True, 0.50),
            ("KXTEST", "bid", 0, 0.50),
            ("KXTEST", "bid", 1, float("nan")),
            ("KXTEST", "bid", 1, 0.55555),
        ]
        for args in invalid:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.client.place_order_v2(*args)
        self.client._req.assert_not_called()

    def test_v2_expiration_requires_gtd_and_future_timestamp(self):
        with patch("bot.kalshi_client.time.time", return_value=1000):
            with self.assertRaises(ValueError):
                self.client.place_order_v2("KXTEST", "bid", 1, 0.50, expiration_time=1200)
            with self.assertRaises(ValueError):
                self.client.place_order_v2(
                    "KXTEST", "bid", 1, 0.50,
                    time_in_force="good_till_canceled", expiration_time=999
                )
        self.client._req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
