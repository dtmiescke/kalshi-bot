import os
import unittest
from unittest.mock import patch

from bot.config import Settings
from bot.engine import _series_sport, _sports_excluded


class TestSportsSelection(unittest.TestCase):
    def test_all_is_default_and_combos_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        self.assertEqual(settings.sports_mode, "all")
        self.assertIsNone(settings.selected_series())
        self.assertFalse(settings.combos_enabled)
        self.assertTrue(settings.combo_research_enabled)
        self.assertEqual(settings.combo_min_legs, 3)
        self.assertEqual(settings.combo_max_legs, 8)

    def test_manual_allowlist_is_normalized(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "manual", "SPORTS_SELECTED": "kxnflgame, KXNBAGAME"}):
            settings = Settings()
        self.assertEqual(settings.selected_series(), {"KXNFLGAME", "KXNBAGAME"})

    def test_excluded_sports_are_classified_before_generic_routing(self):
        cases = [
            ("KXUFCGAME", {"title": "UFC main event"}),
            ("KXBOXING", {"title": "Championship boxing"}),
            ("KXATPMATCH", {"title": "ATP tennis"}),
            ("KXF1RACE", {"title": "Formula 1 Grand Prix"}),
            ("KXPGA", {"title": "PGA golf"}),
            ("KXESPORTS", {"title": "League of Legends"}),
        ]
        for series, metadata in cases:
            with self.subTest(series=series):
                self.assertTrue(_sports_excluded(series, metadata))

    def test_invalid_mode_fails_validation(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "everything"}):
            settings = Settings()
        with self.assertRaises(ValueError):
            settings.validate()


if __name__ == "__main__":
    unittest.main()
