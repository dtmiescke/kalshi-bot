"""Bet slices: proof needs the whole-family bar, and only proven losers can stop a bet."""
import os
import random
import tempfile
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import slices
from bot.storage import Store


class TestSlices(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.now = time.time()
        self.rng = random.Random(5)

    def bet(self, series="KXBTC15M", side="yes", price=0.5, win=True, mins_in=5.0, style="taker", age=3600):
        ts = self.now - age
        close = datetime.fromtimestamp(ts + (15.0 - mins_in) * 60, ZoneInfo("America/New_York"))
        ticker = f"{series}-{close.strftime('%y%b%d%H%M').upper()}-45"
        stake = 10.0
        pnl = stake * (1 - price) / price if win else -stake
        self.store.q("""INSERT INTO bets (ts, mode, ticker, event_ticker, series, side, price, contracts, stake, fee, status, pnl, style)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (ts, "paper", ticker, ticker, series, side, price, 10, stake, 0.1, "graded", pnl, style))

    def test_empty_store_is_safe(self):
        t = slices.table(self.store, now=self.now)
        self.assertEqual(t["bets"], 0)
        self.assertIsNone(slices.blocked(t, "KXBTC15M", "yes", 0.5, 5.0))

    def test_a_clear_loser_is_blocked_and_others_are_not(self):
        for _ in range(60):
            self.bet(series="KXBNB15M", price=0.3, win=self.rng.random() < 0.03, mins_in=self.rng.uniform(0, 14.9))   # BNB loses badly
        for _ in range(400):
            self.bet(series="KXBTC15M", price=0.5, win=self.rng.random() < 0.5, mins_in=self.rng.uniform(0, 14.9))   # BTC is a coin flip at fair odds
        t = slices.table(self.store, now=self.now)
        by = {(s["dim"], s["label"]): s for s in t["slices"]}
        self.assertEqual(by[("coin", "BNB")]["state"], "losing")
        self.assertNotEqual(by[("coin", "BTC")]["state"], "losing")
        self.assertIn("BNB", slices.blocked(t, "KXBNB15M", "yes", 0.5, 5.0))
        self.assertIn("25–40", slices.blocked(t, "KXBTC15M", "yes", 0.3, 5.0))              # the cheap band lost too
        self.assertIsNone(slices.blocked(t, "KXBTC15M", "yes", 0.5, 5.0))

    def test_too_few_bets_never_block(self):
        for _ in range(10):
            self.bet(series="KXSOL15M", win=False)
        t = slices.table(self.store, now=self.now)
        self.assertIsNone(slices.blocked(t, "KXSOL15M", "yes", 0.5, 5.0))

    def test_old_losses_age_out_so_a_blocked_kind_is_retested(self):
        for _ in range(60):
            self.bet(series="KXBNB15M", win=False, age=8 * 86400)
        self.assertEqual(slices.table(self.store, now=self.now)["bets"], 0)

    def test_price_and_time_slices(self):
        for _ in range(60):
            self.bet(price=0.85, win=self.rng.random() < 0.5, mins_in=1.0)                # expensive and early, loses
        t = slices.table(self.store, now=self.now)
        t = {**t, "slices": [x for x in t["slices"] if x["dim"] != "session"]}                # judge price and timing on their own
        self.assertIn("75–100", slices.blocked(t, "KXETH15M", "yes", 0.9, 8.0, now=self.now))
        self.assertIn("first 3 min", slices.blocked(t, "KXETH15M", "yes", 0.5, 1.0, now=self.now))
        self.assertIsNone(slices.blocked(t, "KXETH15M", "yes", 0.5, 8.0, now=self.now))      # that price and point in the window are fine

    def test_the_time_of_day_session_is_a_slice_and_can_block(self):
        for _ in range(240):
            self.bet(series="KXSOL15M", price=0.5, win=self.rng.random() < 0.05, mins_in=self.rng.uniform(0, 14.9), age=self.rng.uniform(60, 3 * 86400))
        t = slices.table(self.store, now=self.now)
        sessions = {s["label"]: s for s in t["slices"] if s["dim"] == "session"}
        self.assertTrue(sessions and sum(s["n"] for s in sessions.values()) == 240)
        self.assertTrue(any(s["state"] == "losing" for s in sessions.values()))
        busy = max(sessions.values(), key=lambda s: s["n"])["label"]
        hour = {"Asia": 3, "Europe": 9, "US": 15, "Late": 22}[busy.split()[0]]
        at = self.now - self.now % 86400 + hour * 3600
        reasons = [slices.blocked(t, "KXBTC15M", "yes", 0.5, 8.0, now=at)]
        self.assertTrue(any(reasons) or all(s["state"] != "losing" for s in sessions.values() if s["label"] == busy))


if __name__ == "__main__":
    unittest.main()
