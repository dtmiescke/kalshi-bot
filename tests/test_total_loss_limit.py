"""Regression tests for the configured bankroll, stake floor, and lifetime loss stop."""
import os
import unittest
from unittest.mock import patch

from bot.config import Settings
from bot.guard import total_loss_tripped


class FakeStore:
    def __init__(self, pnl):
        self.pnl = pnl
        self.calls = []

    def q(self, sql, params=()):
        self.calls.append((sql, params))
        return [{"p": self.pnl}]


class TestRequestedRiskSettings(unittest.TestCase):
    def test_requested_defaults_and_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(mode="paper")
            self.assertEqual(s.bankroll, 200000.0)
            self.assertEqual(s.min_per_bet, 25.0)
            self.assertEqual(s.max_per_bet, 100.0)
            self.assertEqual(s.max_total_loss, 20000.0)
            self.assertEqual(s.max_per_game, 750.0)
            self.assertEqual(s.max_per_day, 20000.0)
            self.assertEqual(s.max_open_cost, 3750.0)
            self.assertEqual(s.daily_loss_limit, 0.0)
            self.assertEqual(s.crypto_min_bet, 25.0)
            self.assertEqual(s.crypto_max_per_bet, 100.0)
            self.assertEqual(s.crypto_max_per_window, 100.0)
            s.validate()

    def test_minimum_cannot_exceed_maximum(self):
        with patch.dict(os.environ, {"MIN_PER_BET": "751", "MAX_PER_BET": "750"}):
            with self.assertRaisesRegex(ValueError, "MIN_PER_BET"):
                Settings(mode="paper").validate()

    def test_lifetime_loss_stop_trips_at_or_beyond_limit(self):
        self.assertFalse(total_loss_tripped(FakeStore(-19999.99), limit=20000, mode="paper"))
        self.assertTrue(total_loss_tripped(FakeStore(-20000), limit=20000, mode="paper"))
        self.assertTrue(total_loss_tripped(FakeStore(-25000), limit=20000, mode="paper"))

    def test_lifetime_loss_is_isolated_by_mode(self):
        store = FakeStore(-20000)
        self.assertTrue(total_loss_tripped(store, limit=20000, mode="paper"))
        self.assertEqual(store.calls[-1][1], ("paper", 0))   # no STATS_SINCE: all history

    def test_invalid_lifetime_limit_fails_closed(self):
        self.assertTrue(total_loss_tripped(FakeStore(0), limit=float("nan"), mode="paper"))
        self.assertTrue(total_loss_tripped(FakeStore(0), limit=0, mode="paper"))


if __name__ == "__main__":
    unittest.main()
