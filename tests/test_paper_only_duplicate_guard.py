"""Regression tests for the hard paper-only default and final duplicate-entry guard."""
import os
import unittest
from unittest import mock
from unittest.mock import patch

from bot.config import Settings
from bot.engine import Engine


class PaperOnlyLockTests(unittest.TestCase):
    def test_environment_cannot_enable_demo_or_live(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "live",
            "LIVE_TRADING_CONFIRMED": "yes",
        }):
            settings = Settings()
        self.assertEqual(settings.mode, "paper")
        self.assertFalse(settings.live_confirmed)


class DuplicateEntryGuardTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.s = mock.Mock()
        self.engine.s.mode = "paper"
        self.engine.store = mock.Mock()
        self.engine.store.has_open_event_bet.return_value = False
        self.engine.store.q.return_value = []

    def test_missing_event_ticker_fails_closed(self):
        reason = self.engine._duplicate_entry_reason({
            "ticker": "KXBTC15M-TEST", "series": "KXBTC15M"
        })
        self.assertIn("missing event ticker", reason)
        self.engine.store.has_open_event_bet.assert_not_called()

    def test_open_event_position_blocks_second_entry(self):
        self.engine.store.has_open_event_bet.return_value = True
        reason = self.engine._duplicate_entry_reason({
            "ticker": "KXBTC15M-TEST", "event_ticker": "BTC-WINDOW",
            "series": "KXBTC15M"
        })
        self.assertIn("open bet", reason)
        self.engine.store.q.assert_not_called()

    def test_settled_or_closed_crypto_window_still_blocks_reentry(self):
        self.engine.store.q.return_value = [{"1": 1}]
        reason = self.engine._duplicate_entry_reason({
            "ticker": "KXBTC15M-TEST", "event_ticker": "BTC-WINDOW",
            "series": "KXBTC15M"
        })
        self.assertIn("already traded", reason)
        self.engine.store.q.assert_called_once_with(
            "SELECT 1 FROM bets WHERE event_ticker=? AND mode=? LIMIT 1",
            ("BTC-WINDOW", "paper"),
        )

    def test_fresh_event_with_no_prior_position_is_allowed(self):
        reason = self.engine._duplicate_entry_reason({
            "ticker": "KXBTC15M-TEST", "event_ticker": "BTC-NEW-WINDOW",
            "series": "KXBTC15M"
        })
        self.assertIsNone(reason)

    def test_open_sports_event_position_blocks_duplicate(self):
        self.engine.store.has_open_event_bet.return_value = True
        reason = self.engine._duplicate_entry_reason({
            "ticker": "KXNBA-GAME-YES", "event_ticker": "NBA-GAME",
            "series": "KXNBAGAME"
        })
        self.assertIn("open bet", reason)


if __name__ == "__main__":
    unittest.main()
