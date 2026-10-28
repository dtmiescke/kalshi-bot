"""Bitstamp and Gemini join Kraken and Coinbase in the live composite price:
Kalshi settles 15-minute markets on CF Benchmarks indices built from several
constituent exchanges."""
import unittest

from bot.live import feeds


class A:
    def __init__(self):
        self.refs = []

    def add_reference(self, exchange, ts, price):
        self.refs.append((exchange, ts, price))


class TestBitstamp(unittest.TestCase):
    def test_book_snapshot_gives_mid(self):
        a = A()
        feeds.handle_bitstamp({"event": "data", "channel": "order_book_solusd",
                               "data": {"microtimestamp": "1793191888568362",
                                        "bids": [["101.462", "54.7"]], "asks": [["101.464", "22.9"]]}},
                              {"solusd": a})
        self.assertEqual(a.refs[0][0], "bitstamp")
        self.assertAlmostEqual(a.refs[0][1], 1793191888.568362, places=3)
        self.assertAlmostEqual(a.refs[0][2], 101.463)

    def test_ignores_other_messages_and_crossed_books(self):
        a = A()
        feeds.handle_bitstamp({"event": "bts:subscription_succeeded", "channel": "order_book_solusd", "data": {}}, {"solusd": a})
        feeds.handle_bitstamp({"event": "data", "channel": "order_book_solusd",
                               "data": {"bids": [["102", "1"]], "asks": [["101", "1"]]}}, {"solusd": a})
        self.assertEqual(a.refs, [])

    def test_subscribes_to_book_channels(self):
        self.assertEqual(feeds.bitstamp_subs(["btcusd"])[0]["data"]["channel"], "order_book_btcusd")


class TestGemini(unittest.TestCase):
    def test_best_levels_tracked_through_snapshot_and_changes(self):
        books, a = feeds.GeminiBooks(), A()
        snap = {"type": "l2_updates", "symbol": "BTCUSD",
                "changes": [["buy", "100", "1"], ["buy", "99", "2"], ["sell", "102", "1"], ["sell", "103", "5"]]}
        feeds.handle_gemini(snap, {"BTCUSD": a}, books, now=10)
        self.assertEqual(a.refs[-1][2], 101.0)
        # best bid removed -> next level; better ask added
        feeds.handle_gemini({"type": "l2_updates", "symbol": "BTCUSD",
                             "changes": [["buy", "100", "0"], ["sell", "101.5", "2"]]}, {"BTCUSD": a}, books, now=11)
        self.assertEqual(a.refs[-1][2], (99 + 101.5) / 2)

    def test_throttled_and_ignores_heartbeats(self):
        books, a = feeds.GeminiBooks(), A()
        m = {"type": "l2_updates", "symbol": "BTCUSD", "changes": [["buy", "100", "1"], ["sell", "101", "1"]]}
        feeds.handle_gemini(m, {"BTCUSD": a}, books, now=10.0)
        feeds.handle_gemini(m, {"BTCUSD": a}, books, now=10.1)      # within 0.25 s
        feeds.handle_gemini({"type": "heartbeat"}, {"BTCUSD": a}, books, now=11)
        self.assertEqual(len(a.refs), 1)


if __name__ == "__main__":
    unittest.main()
