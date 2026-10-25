"""Regression coverage for existing sports-selection modes (no UI or MCP changes)."""
import os
import unittest
from unittest.mock import patch

from bot.config import Settings, SPORTS


class TestSportsSelection(unittest.TestCase):
    def test_configured_keeps_original_five(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "configured"}, clear=False):
            chosen = Settings().selected_series()
        self.assertEqual(chosen, {
            "KXNFLGAME", "KXNCAAFGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME",
        })
        self.assertTrue(chosen.issubset(SPORTS))

    def test_manual_respects_explicit_selection(self):
        with patch.dict(os.environ, {
            "SPORTS_MODE": "manual", "SPORTS_SELECTED": "KXEPLGAME,KXNFLGAME"
        }, clear=False):
            self.assertEqual(Settings().selected_series(), {"KXEPLGAME", "KXNFLGAME"})

    def test_all_remains_opt_in(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "all"}, clear=False):
            self.assertIsNone(Settings().selected_series())


if __name__ == "__main__":
    unittest.main()
