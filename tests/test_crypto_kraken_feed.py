"""Regression tests for the Kraken-only BNB spot adapter."""
import unittest
from unittest.mock import patch

from bot.signals import crypto


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TestKrakenCryptoFeeds(unittest.TestCase):
    def setUp(self):
        crypto._KRAKEN_CANDLE_CACHE.clear()

    @patch("bot.signals.crypto.requests.get")
    def test_kraken_candles_parse_ohlc_and_skip_pagination_cursor(self, get):
        get.return_value = _Response({
            "error": [],
            "result": {
                "BNBUSD": [[1000, "600", "610", "590", "605", "602", "12.5", 30]],
                "last": 1000,
            },
        })
        candles = crypto.fetch_kraken_candles("BNB/USD")
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0], {
            "ts": 1000, "open": 600.0, "high": 610.0, "low": 590.0,
            "close": 605.0, "volume": 12.5,
        })
        self.assertEqual(get.call_args.kwargs["params"], {"pair": "BNBUSD", "interval": 1})

    @patch("bot.signals.crypto.requests.get")
    def test_kraken_spot_returns_last_trade_price(self, get):
        get.return_value = _Response({
            "error": [],
            "result": {"XBNBUSD": {"c": ["605.25", "1.0"]}},
        })
        spot, ts = crypto.fetch_kraken_spot("BNB/USD")
        self.assertEqual(spot, 605.25)
        self.assertIsInstance(ts, float)

    @patch("bot.signals.crypto.requests.get")
    def test_kraken_feed_fails_closed_on_api_error(self, get):
        get.return_value = _Response({"error": ["EQuery:Unknown asset pair"], "result": {}})
        self.assertEqual(crypto.fetch_kraken_candles("BNB/USD"), [])
        self.assertEqual(crypto.fetch_kraken_spot("BNB/USD"), (None, None))


if __name__ == "__main__":
    unittest.main()
