"""15-minute markets must be graded on the pass after they settle, not whenever an alphabetical sweep of thousands of
sports futures reaches them (that took about 3.5 hours, so every downstream number lagged by that much)."""
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import grader
from bot.storage import Store


class Client:
    """Crypto 15-minute markets are settled; the sports futures are not."""

    def market(self, ticker):
        if "15M-" in ticker:
            return {"ticker": ticker, "series_ticker": ticker.split("-")[0], "result": "yes"}
        return {"ticker": ticker, "series_ticker": "KXNBASEED", "result": ""}


def add_readings(store, ticker, start, settle_after):
    for signal in ("crypto_model", "kalshi_market"):
        store.x("INSERT INTO signal_readings(ts,ticker,signal,prob,confidence,detail) VALUES(?,?,?,?,?,?)",
                (1, ticker, signal, 0.6, 0.8, "{}"))
    store.set_kv(f"start_{ticker}", start)
    store.set_kv(f"settle_after_{ticker}", settle_after)


class TestGradingPriority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def graded(self):
        return {r["ticker"] for r in self.store.q("SELECT ticker FROM graded_markets")}

    def test_settled_15_minute_markets_are_graded_even_when_the_rotation_is_far_away(self):
        for i in range(400):                                          # a big backlog of futures that never settle
            add_readings(self.store, f"KXNBASEED-27-T{i:03d}", 10**12, 10**12 + 1)
        self.store.set_kv("learning_cursor_ticker", "KXNBASEED-27-T000")    # 399 futures still ahead of the sweep
        crypto = ["KXBNB15M-26OCT291530-30", "KXBTC15M-26OCT291545-45", "KXSOL15M-26OCT291700-00"]
        for t in crypto:
            add_readings(self.store, t, 10_000, 10_001)
        with patch("bot.grader.ml.label"), patch("bot.grader.brain.learn", return_value={}):
            n, _ = grader.learn_from_markets(self.store, Client(), max_fetch=150)
        self.assertEqual(n, 3)
        self.assertEqual(self.graded(), set(crypto))

    def test_the_rotation_cursor_still_follows_the_rotation_only(self):
        for i in range(10):
            add_readings(self.store, f"KXNBASEED-27-T{i:03d}", 10**12, 10**12 + 1)
        add_readings(self.store, "KXBTC15M-26OCT291545-45", 10_000, 10_001)
        with patch("bot.grader.ml.label"), patch("bot.grader.brain.learn", return_value={}):
            grader.learn_from_markets(self.store, Client(), max_fetch=150)
        # the cursor is the last ticker of the rotating batch, never the priority crypto ticker
        self.assertEqual(self.store.get_kv("learning_cursor_ticker"), "KXNBASEED-27-T009")

    def test_an_unsettled_15_minute_market_is_left_alone_until_it_is_due(self):
        add_readings(self.store, "KXETH15M-26OCT292345-45", 10**12, 10**12 + 1)     # window in the future
        with patch("bot.grader.ml.label"), patch("bot.grader.brain.learn", return_value={}):
            n, _ = grader.learn_from_markets(self.store, Client(), max_fetch=150)
        self.assertEqual(n, 0)
        self.assertEqual(self.graded(), set())

    def test_already_graded_markets_are_not_graded_twice(self):
        add_readings(self.store, "KXBTC15M-26OCT291545-45", 10_000, 10_001)
        with patch("bot.grader.ml.label"), patch("bot.grader.brain.learn", return_value={}):
            self.assertEqual(grader.learn_from_markets(self.store, Client(), max_fetch=150)[0], 1)
            self.assertEqual(grader.learn_from_markets(self.store, Client(), max_fetch=150)[0], 0)


if __name__ == "__main__":
    unittest.main()
