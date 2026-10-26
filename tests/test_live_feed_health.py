"""Regression tests for shared live-feed health and fail-closed readiness."""
import unittest
from unittest.mock import Mock

from bot.live.runner import LiveRunner
from bot.live.kalshi_book import KalshiBook


class TestLiveFeedHealth(unittest.TestCase):
    def make_runner(self):
        runner = LiveRunner.__new__(LiveRunner)
        runner.s = Mock(mode="demo")
        runner.store = Mock()
        runner.store.get_kv.return_value = None
        runner.summary = {}
        runner.betting_ok = True
        runner.books = {}
        runner.ticker_updates = {}
        runner.ws_connected = True
        return runner

    def test_fresh_book_publishes_ready_health(self):
        runner = self.make_runner()
        book = KalshiBook("KXTEST")
        book.snapshot({"yes_dollars_fp": [["0.40", "5"]],
                       "no_dollars_fp": [["0.50", "4"]]}, seq=1)
        runner.books["KXTEST"] = book
        health = runner.publish_feed_health(now=book.last_update + 0.5)
        self.assertEqual(health["fresh_books"], 1)
        self.assertEqual(health["stale_book_count"], 0)
        self.assertTrue(health["trading_ready"])
        runner.store.set_kv.assert_called_once_with("live_feed_health", health)

    def test_stale_or_invalid_book_fails_closed(self):
        runner = self.make_runner()
        book = KalshiBook("KXTEST")
        runner.books["KXTEST"] = book
        health = runner.publish_feed_health(now=100.0)
        self.assertEqual(health["stale_book_count"], 1)
        self.assertFalse(health["trading_ready"])

    def test_unresolved_execution_reconciliation_blocks_readiness(self):
        runner = self.make_runner()
        book = KalshiBook("KXTEST")
        book.snapshot({"yes_dollars_fp": [["0.40", "5"]],
                       "no_dollars_fp": [["0.50", "4"]]}, seq=1)
        runner.books["KXTEST"] = book
        runner.store.get_kv.return_value = "unknown order outcome"
        health = runner.publish_feed_health(now=book.last_update + 0.5)
        self.assertTrue(health["execution_reconciliation_required"])
        self.assertFalse(health["trading_ready"])


if __name__ == "__main__":
    unittest.main()
