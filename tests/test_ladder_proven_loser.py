import os
import tempfile
import time
import unittest

from bot import guard
from bot.storage import Store


def put(store, series, pnl, stake=10.0, mode="paper", ago=3600):
    ts = int(time.time()) - ago
    store.x("""INSERT INTO bets (ts, mode, ticker, event_ticker, series, team, side, price, contracts, stake, fee,
                  model_prob, edge, confidence, signals_json, order_id, status, pnl, graded_ts)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (ts, mode, f"{series}-{ts}-{pnl}-{ago}", "E", series, "x", "yes", 0.5, 20, stake, 0.1,
                0.6, 0.1, 0.5, "{}", None, "graded", pnl, ts))


class TestProvenLoser(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def test_steady_losses_trip_the_gate(self):
        for i in range(25):
            put(self.store, "KXBTCD" if i % 2 else "KXETHD", -6.0 + (i % 3), ago=100 + i)
        self.assertTrue(guard.proven_loser(self.store, ["KXBTCD", "KXETHD"], mode="paper"))

    def test_too_few_bets_never_trips(self):
        for i in range(10):
            put(self.store, "KXBTCD", -9.0, ago=100 + i)
        self.assertFalse(guard.proven_loser(self.store, ["KXBTCD", "KXETHD"], mode="paper"))

    def test_noisy_or_winning_results_do_not_trip(self):
        for i in range(30):
            put(self.store, "KXBTCD", 8.0 if i % 2 else -9.0, ago=100 + i)    # a coin flip around zero
        self.assertFalse(guard.proven_loser(self.store, ["KXBTCD"], mode="paper"))
        for i in range(30):
            put(self.store, "KXETHD", 4.0, ago=100 + i)
        self.assertFalse(guard.proven_loser(self.store, ["KXETHD"], mode="paper"))

    def test_old_losses_roll_off_and_other_modes_are_ignored(self):
        for i in range(25):
            put(self.store, "KXBTCD", -8.0, ago=400 * 86400 + i)               # far outside the recovery window
            put(self.store, "KXBTCD", -8.0, mode="live", ago=100 + i)
        self.assertFalse(guard.proven_loser(self.store, ["KXBTCD"], mode="paper"))


if __name__ == "__main__":
    unittest.main()
