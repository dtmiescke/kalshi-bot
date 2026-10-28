"""Entries are skipped in a stretch of the window only when settled windows show they lose money."""
import os
import random
import tempfile
import unittest
from unittest import mock

from bot import entry_timing
from bot.storage import Store

NOW = 1_793_250_000


def ticker_closing(minute_offset, serial):
    """A 15-minute ticker that closes at NOW - minute_offset minutes (Eastern stamp, 4 hours behind UTC in October)."""
    import datetime as dt
    close = dt.datetime.fromtimestamp(NOW - minute_offset * 60 - (NOW % 900) , dt.timezone.utc)
    et = close - dt.timedelta(hours=4)
    return close, f"KXBTC15M-{et.strftime('%y%b%d%H%M').upper()}-{et.strftime('%M')}"


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.d.cleanup()

    def window(self, i, mins_left, p, k, y):
        """One graded research snapshot taken mins_left before window i closes."""
        close = NOW - 900 * (i + 1)
        close -= close % 900
        import datetime as dt
        et = dt.datetime.fromtimestamp(close, dt.timezone.utc) - dt.timedelta(hours=4)
        ticker = f"KXBTC15M-{et.strftime('%y%b%d%H%M').upper()}-{et.strftime('%M')}"
        self.st.x("""INSERT INTO decision_audit(ts,ticker,event_ticker,series,decision,market_price,model_prob,outcome,brier)
                     VALUES(?,?,?,?,?,?,?,?,?)""", (int(close - mins_left * 60), ticker, ticker, "KXBTC15M", "research", k, p, y, (p - y) ** 2))
        return ticker


class TestTable(Base):
    def test_ticker_close_time_is_eastern(self):
        self.assertEqual(entry_timing.close_ts("KXBTC15M-26OCT282345-45"), 1793245500.0)
        self.assertIsNone(entry_timing.close_ts("KXNFLGAME-26OCT29CARGB"))

    def test_buckets_cover_the_betting_window(self):
        self.assertEqual(entry_timing.bucket_index(13.0), 0)
        self.assertEqual(entry_timing.bucket_index(9.0), 2)
        self.assertEqual(entry_timing.bucket_index(2.5), 5)
        self.assertIsNone(entry_timing.bucket_index(1.0))
        self.assertIsNone(entry_timing.bucket_index(14.5))

    def test_a_stretch_that_keeps_losing_is_blocked_and_the_others_stay_open(self):
        rng = random.Random(1)
        for i in range(120):
            # forecast says YES at 75% against a 50% price (a 25 point 'edge') but YES wins only 20% of the time here
            self.window(i, 5.0, 0.75, 0.50, int(rng.random() < 0.2))        # the 6-4 minute stretch
        for i in range(120, 240):
            self.window(i, 11.0, 0.75, 0.50, int(rng.random() < 0.80))      # the 12-10 stretch, where it is right
        t = entry_timing.table(self.st, now=NOW)
        by = {(b["from"], b["to"]): b for b in t["buckets"]}
        self.assertEqual(by[(6.0, 4.0)]["state"], "blocked")
        self.assertIn(by[(12.0, 10.0)]["state"], ("open", "favoured"))
        self.assertEqual(by[(8.0, 6.0)]["state"], "learning")                # no windows: left alone
        self.assertFalse(entry_timing.allowed(t, 5.0))
        self.assertTrue(entry_timing.allowed(t, 11.0))
        self.assertTrue(entry_timing.allowed(t, 7.0))
        self.assertTrue(entry_timing.allowed(None, 5.0))

    def test_a_modest_loss_on_a_modest_sample_is_not_enough_to_block(self):
        rng = random.Random(2)
        for i in range(120):                                               # a coin flip costs about 3 cents a contract
            self.window(i, 5.0, 0.75, 0.50, int(rng.random() < 0.5))
        t = entry_timing.table(self.st, now=NOW)
        self.assertEqual({b["state"] for b in t["buckets"] if b["n"]}, {"open"})     # not proven bad, so left open

    def test_few_windows_never_block(self):
        for i in range(30):
            self.window(i, 5.0, 0.80, 0.50, 0)                             # 30 straight losses is still too few
        t = entry_timing.table(self.st, now=NOW)
        self.assertEqual({b["state"] for b in t["buckets"]}, {"learning"})

    def test_only_the_first_qualifying_snapshot_of_a_window_counts_and_ladders_are_ignored(self):
        tk = self.window(0, 5.0, 0.80, 0.50, 0)
        self.window(0, 5.0, 0.80, 0.50, 0)                                   # a second snapshot of the same window
        self.st.x("""INSERT INTO decision_audit(ts,ticker,event_ticker,series,decision,market_price,model_prob,outcome,brier)
                     VALUES(?,?,?,?,?,?,?,?,?)""", (NOW - 600, "KXBTCD-26OCT2812-T80000", "L", "KXBTCD", "research", .5, .9, 0, .8))
        t = entry_timing.table(self.st, now=NOW)
        self.assertEqual(sum(b["n"] for b in t["buckets"]), 1)
        self.assertEqual(t["windows"], 1)

    def test_the_summary_is_what_the_app_reads(self):
        s = entry_timing.summary(self.st)
        self.assertEqual(len(s["buckets"]), 6)
        self.assertEqual(s["min_windows"], entry_timing.MIN_WINDOWS)


class TestRunnerGate(Base):
    def test_the_gate_can_be_switched_off_and_defaults_to_the_table(self):
        from bot.live.runner import LiveRunner
        from tests.test_live import FakeRest
        import time
        with mock.patch.dict(os.environ, {"BOT_DB": os.path.join(self.d.name, "g.db")}):
            from bot.config import Settings
            s = Settings()
            r = LiveRunner(s, Store(s.db_path), rest=FakeRest(time.time() + 600), llm=None, seed=False, with_context=False)
            blocked = {"buckets": [{"from": h, "to": l, "n": 99, "mean": -.05, "lcb": -.08, "ucb": -.01, "state": "blocked"}
                                   for h, l in entry_timing.BUCKETS], "windows": 99}
            r._et = (time.time(), blocked)
            self.assertFalse(entry_timing.allowed(r.entry_table(time.time()), 9.0))


if __name__ == "__main__":
    unittest.main()
