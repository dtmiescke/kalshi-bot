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

    def test_all_sports_are_researchable_and_classified_before_generic_routing(self):
        cases = [
            ("KXUFCGAME", {"title": "UFC main event"}, "mma"),
            ("KXBOXING", {"title": "Championship boxing"}, "boxing"),
            ("KXATPMATCH", {"title": "ATP tennis"}, "tennis"),
            ("KXF1RACE", {"title": "Formula 1 Grand Prix"}, "motorsports"),
            ("KXPGA", {"title": "PGA golf"}, "golf"),
            ("KXESPORTS", {"title": "League of Legends"}, "esports"),
        ]
        for series, metadata, category in cases:
            with self.subTest(series=series):
                self.assertEqual(_series_sport(series, metadata), category)
                self.assertFalse(_sports_excluded(series, metadata))

    def test_manual_selection_keeps_nontraditional_sports(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "manual",
                                    "SPORTS_SELECTED": "KXATPMATCH,KXF1RACE,KXESPORTS"}):
            settings = Settings()
        selected = settings.selected_series()
        series_map = {series: _series_sport(series) for series in selected}
        self.assertEqual(set(series_map), {"KXATPMATCH", "KXF1RACE", "KXESPORTS"})

    def test_manual_mode_requires_at_least_one_series(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "manual", "SPORTS_SELECTED": ""}):
            settings = Settings()
        with self.assertRaisesRegex(ValueError, "requires at least one"):
            settings.validate()

    def test_combo_execution_toggle_fails_closed_until_execution_exists(self):
        with patch.dict(os.environ, {"COMBOS_ENABLED": "yes"}):
            settings = Settings()
        with self.assertRaisesRegex(ValueError, "not implemented"):
            settings.validate()

    def test_negative_combo_audit_lead_is_rejected(self):
        with patch.dict(os.environ, {"COMBO_AUDIT_LEAD_SECONDS": "-1"}):
            settings = Settings()
        with self.assertRaisesRegex(ValueError, "non-negative"):
            settings.validate()

    def test_invalid_mode_fails_validation(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "everything"}):
            settings = Settings()
        with self.assertRaises(ValueError):
            settings.validate()


if __name__ == "__main__":
    unittest.main()
