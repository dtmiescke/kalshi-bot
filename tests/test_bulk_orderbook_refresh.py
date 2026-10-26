"""Offline tests for the bounded bulk orderbook refresh path."""
import time
import unittest
from unittest.mock import Mock

from bot.live.kalshi_book import KalshiBook
from bot.live.runner import LiveRunner


class TestBulkBookRefresh(unittest.TestCase):
    def runner(self, rest, tickers):
        runner = LiveRunner.__new__(LiveRunner)
        runner.rest = rest
        runner.books = {ticker: KalshiBook(ticker) for ticker in tickers}
        runner.ws_connected = True
        return runner

    def test_bulk_refresh_updates_stale_books(self):
        rest = Mock()
        rest.market_orderbooks.return_value = {
            "A": {"yes": [(0.42, 5.0)], "no": [(0.51, 4.0)]},
            "B": {"yes": [(0.31, 7.0)], "no": [(0.60, 2.0)]},
        }
        runner = self.runner(rest, ["A", "B"])
        runner.poll_books(only_quiet=True)
        rest.market_orderbooks.assert_called_once_with(["A", "B"])
        self.assertEqual(runner.books["A"].yes, {0.42: 5.0})
        self.assertEqual(runner.books["B"].no, {0.60: 2.0})
        self.assertFalse(runner.books["A"].needs_resync)

    def test_bulk_missing_ticker_falls_back_to_single_market(self):
        rest = Mock()
        rest.market_orderbooks.return_value = {"A": {"yes": [(0.42, 5)], "no": [(0.51, 4)]}}
        rest.orderbook.return_value = {"yes": [(0.30, 2)], "no": [(0.65, 3)]}
        runner = self.runner(rest, ["A", "B"])
        runner.poll_books(only_quiet=True)
        rest.orderbook.assert_called_once_with("B")
        self.assertEqual(runner.books["B"].yes, {0.30: 2})

    def test_bad_bulk_snapshot_does_not_mark_book_fresh(self):
        rest = Mock()
        rest.market_orderbooks.return_value = {"A": {"yes": [(float("nan"), 1)], "no": []}}
        rest.orderbook.side_effect = RuntimeError("offline")
        runner = self.runner(rest, ["A"])
        runner.books["A"].needs_resync = True
        runner.poll_books(only_resync=True)
        self.assertTrue(runner.books["A"].needs_resync)
        self.assertEqual(runner.books["A"].gaps, 1)

    def test_fresh_websocket_book_is_not_refreshed_in_quiet_mode(self):
        rest = Mock()
        runner = self.runner(rest, ["A"])
        runner.books["A"].snapshot({"yes": [[0.4, 2]], "no": [[0.5, 3]]})
        runner.poll_books(only_quiet=True)
        rest.market_orderbooks.assert_not_called()
        rest.orderbook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
