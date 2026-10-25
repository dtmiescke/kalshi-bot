import unittest

from bot.signals.crypto_microstructure import indicator_snapshot, orderbook, summarize_book


class FakeStore:
    def __init__(self):
        self.data = {}

    def get_kv(self, key, default=None):
        return self.data.get(key, default)

    def set_kv(self, key, value):
        self.data[key] = value


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CryptoMicrostructureTests(unittest.TestCase):
    def setUp(self):
        self.book = {
            "bids": [["100.0", "8", 1], ["99.9", "4", 1]],
            "asks": [["100.2", "2", 1], ["100.3", "2", 1]],
        }

    def test_summarize_book_reports_spread_and_depth_imbalance(self):
        result = summarize_book(self.book)
        self.assertIsNotNone(result)
        self.assertEqual(result["venue"], "coinbase")
        self.assertGreater(result["spread_bps"], 0)
        self.assertGreater(result["depth_imbalance"], 0)
        self.assertLessEqual(abs(result["depth_imbalance"]), 1)

    def test_summarize_book_rejects_crossed_or_empty_books(self):
        self.assertIsNone(summarize_book({"bids": [], "asks": []}))
        self.assertIsNone(summarize_book({
            "bids": [["101", "1"]], "asks": [["100", "1"]]}))

    def test_orderbook_uses_five_second_cache(self):
        store = FakeStore()
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return FakeResponse(self.book)
        first = orderbook(store, "BTC", get=get, now=100)
        second = orderbook(store, "BTC", get=get, now=103)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertIn("BTC-USD", calls[0])

    def test_orderbook_fails_closed_on_network_error(self):
        store = FakeStore()
        def get(*args, **kwargs):
            raise OSError("offline")
        self.assertIsNone(orderbook(store, "DOGE", get=get, now=100))
        self.assertIsNone(orderbook(store, "BTC", get=get, now=100))

    def test_indicator_snapshot_only_exports_existing_numeric_features(self):
        result = indicator_snapshot({
            "rsi_1m": 62.0, "ema_9_21": 0.2, "model_prob": 0.7,
            "bb_pctb": 1.1, "untrusted": "ignored",
        })
        self.assertEqual(result, {"rsi_1m": 62.0, "ema_9_21": 0.2, "bb_pctb": 1.1})


if __name__ == "__main__":
    unittest.main()
