"""Reads before the market opens, and finding the new windows right at the mark."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from bot import brain
from bot.config import Settings
from bot.live.runner import LiveRunner
from bot.signals.base import Reading
from bot.storage import Store
from tests.test_live import FakeRest

T = 1_793_187_000 - (1_793_187_000 % 900)       # a quarter-hour mark


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BOT_DB": os.path.join(self.d.name, "b.db")})
        self.env.start()
        self.s = Settings()
        self.st = Store(self.s.db_path)
        self.r = LiveRunner(self.s, self.st, rest=FakeRest(T + 900), llm=None, seed=False, with_context=False)

    def tearDown(self):
        self.env.stop()
        self.d.cleanup()

    def fill_chart(self, now):
        a = self.r.coins["KXBTC15M"]
        a.book.snapshot([(100290, 1)], [(100310, 1)], now)
        for i in range(3600, 0, -1):
            a.mids.append((now - i, 100300 + (i % 7) * 3 - 9))
        start = int(now) - int(now) % 60 - 3600 - 60
        self.r.seed_candles["KXBTC15M"] = [      # the history the live bot seeds from Coinbase
            {"ts": start - 60 * i, "open": 100300.0, "high": 100330.0, "low": 100270.0,
             "close": 100300.0 + (i % 5) * 4, "volume": 5.0} for i in range(120, 0, -1)]


class TestFastDiscovery(Base):
    def test_looks_every_three_seconds_after_the_mark_until_every_coin_has_a_window(self):
        r = self.r
        self.assertTrue(r.new_window_missing(T + 5, T + 1))                  # nothing found yet
        self.assertFalse(r.new_window_missing(T + 5, T + 3))                 # looked 2 s ago
        self.assertTrue(r.new_window_missing(T + 100, T + 90))               # Kalshi activates some windows 30-100 s after the mark
        self.assertFalse(r.new_window_missing(T + 200, T + 190))             # past two and a half minutes: the 60 s sweep covers it
        self.assertFalse(r.new_window_missing(T - 100, T - 110))             # long before a mark
        r.markets = {f"{s}-X": {"series": s, "terms": {"close_ts": T + 900}} for s in r.coins}
        self.assertFalse(r.new_window_missing(T + 5, T))                     # all four found
        r.markets.popitem()
        self.assertTrue(r.new_window_missing(T + 5, T))                      # one still missing
        r.markets = {f"{s}-OLD": {"series": s, "terms": {"close_ts": T}} for s in r.coins}   # the closing window
        self.assertTrue(r.new_window_missing(T - 1 + 1, T - 10))             # does not count as the new one


class TestPreOpen(Base):
    def test_reads_the_chart_in_the_last_20_seconds_before_the_mark(self):
        now = T - 10
        self.fill_chart(now)
        self.r.pre_open_reads(now)
        reads = self.r.pre_open["KXBTC15M"][T]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["s"], -10)
        self.assertIn("rsi_15m", reads[0]["ind"])
        self.r.pre_open_reads(now + 2)                                       # throttled to every 5 s
        self.assertEqual(len(self.r.pre_open["KXBTC15M"][T]), 1)
        self.r.pre_open_reads(now + 6)
        self.assertEqual(len(self.r.pre_open["KXBTC15M"][T]), 2)
        self.assertEqual(self.r.pre_open.get("KXETH15M", {}).get(T, []), [])   # no chart, no read

    def test_nothing_earlier_than_20_seconds_before(self):
        self.fill_chart(T - 60)
        self.r.pre_open_reads(T - 60)
        self.assertEqual(self.r.pre_open, {})

    def test_the_read_is_saved_with_the_window_and_survives_later_reads(self):
        now = T - 10
        self.fill_chart(now)
        self.r.pre_open_reads(now)
        terms = {"strike": 100_300.0, "close_ts": T + 900, "avg_seconds": 60}
        readings = [Reading("kalshi_market", 0.50, 0.9, {})]
        ctx = {"kalshi_yes_bid": 0.49, "kalshi_yes_ask": 0.51, "spot": {}, "features": {}}
        info = {"series": "KXBTC15M"}
        self.r.take_window_read("KXBTC15M-T1", info, terms, T + 3, readings, ctx, brain.DEFAULT_WEIGHTS)
        self.r.take_window_read("KXBTC15M-T1", info, terms, T + 20, readings, ctx, brain.DEFAULT_WEIGHTS)
        snap = json.loads(self.st.q("SELECT snapshot FROM window_log")[0]["snapshot"])
        self.assertEqual(snap["pre_open"][0]["s"], -10)
        self.assertEqual(len(snap["reads"]), 2)


if __name__ == "__main__":
    unittest.main()
