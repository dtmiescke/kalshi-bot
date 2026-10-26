"""Integration contract tests for live-feed -> shared market-state wiring.

These tests are deterministic and offline: they exercise the same pure message
handlers used by the reconnecting WebSocket loops.
"""
import unittest
from unittest.mock import Mock

from bot.live import feeds
from bot.live.kalshi_book import KalshiBook


class TestLiveFeedWiring(unittest.TestCase):
    def test_kalshi_snapshot_then_contiguous_delta_updates_shared_book(self):
        book = KalshiBook("KXTEST")
        books = {"KXTEST": book}
        feeds.handle_kalshi({
            "type": "orderbook_snapshot", "seq": 10,
            "msg": {"market_ticker": "KXTEST",
                    "yes_dollars_fp": [["0.40", "5"]],
                    "no_dollars_fp": [["0.50", "4"]]},
        }, books)
        self.assertFalse(book.needs_resync)
        self.assertEqual(book.yes, {0.4: 5.0})
        self.assertEqual(book.no, {0.5: 4.0})

        feeds.handle_kalshi({
            "type": "orderbook_delta", "seq": 11,
            "msg": {"market_ticker": "KXTEST", "side": "yes",
                    "price_dollars": "0.40", "delta_fp": "-2"},
        }, books)
        self.assertFalse(book.needs_resync)
        self.assertEqual(book.yes, {0.4: 3.0})

    def test_sequence_gap_invalidates_book_until_snapshot(self):
        book = KalshiBook("KXTEST")
        book.snapshot({"yes_dollars_fp": [["0.40", "5"]],
                       "no_dollars_fp": [["0.50", "4"]]}, seq=10)
        feeds.handle_kalshi({
            "type": "orderbook_delta", "seq": 12,
            "msg": {"market_ticker": "KXTEST", "side": "yes",
                    "price_dollars": "0.40", "delta_fp": "-1"},
        }, {"KXTEST": book})
        self.assertTrue(book.needs_resync)
        self.assertIsNone(book.age())
        self.assertEqual(book.gaps, 1)

        feeds.handle_kalshi({
            "type": "orderbook_snapshot", "seq": 20,
            "msg": {"market_ticker": "KXTEST",
                    "yes_dollars_fp": [["0.41", "7"]],
                    "no_dollars_fp": [["0.49", "6"]]},
        }, {"KXTEST": book})
        self.assertFalse(book.needs_resync)
        self.assertEqual(book.seq, 20)
        self.assertEqual(book.yes, {0.41: 7.0})

    def test_ticker_feed_records_receive_timestamp_for_shared_diagnostics(self):
        updates = {}
        feeds.handle_kalshi({
            "type": "ticker",
            "msg": {"market_ticker": "KXTEST", "yes_bid_dollars": "0.41",
                    "yes_ask_dollars": "0.44", "volume_fp": "120"},
        }, {}, updates)
        row = updates["KXTEST"]
        self.assertEqual(row["yes_bid_dollars"], "0.41")
        self.assertEqual(row["yes_ask_dollars"], "0.44")
        self.assertGreater(row["received_ts"], 0)

    def test_untracked_market_messages_do_not_create_books(self):
        books = {}
        feeds.handle_kalshi({
            "type": "orderbook_snapshot", "seq": 1,
            "msg": {"market_ticker": "UNKNOWN",
                    "yes_dollars_fp": [["0.40", "1"]]},
        }, books)
        self.assertEqual(books, {})


if __name__ == "__main__":
    unittest.main()
