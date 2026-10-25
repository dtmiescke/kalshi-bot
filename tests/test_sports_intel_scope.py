"""Configured sports mode must not fetch unrelated sports intelligence feeds."""
import unittest

from bot.config import SPORTS
from bot.sports_intel import categories_for_series, SCOREBOARD_CATEGORIES


class TestSportsIntelScope(unittest.TestCase):
    def test_configured_sports_only_enable_matching_intelligence_categories(self):
        categories = categories_for_series(SPORTS.keys(), SPORTS, mode="configured")
        self.assertEqual(categories, {"football", "basketball", "baseball", "hockey", "soccer"})
        self.assertTrue(categories <= set(SCOREBOARD_CATEGORIES))

    def test_all_mode_is_explicit_opt_in_to_all_intelligence_categories(self):
        self.assertIsNone(categories_for_series(SPORTS.keys(), SPORTS, mode="all"))

    def test_manual_selection_maps_only_selected_additional_sports(self):
        categories = categories_for_series(
            {"KXSOCCER-EPL", "KXTENNIS-ATP"}, {}, mode="manual")
        self.assertEqual(categories, {"soccer", "tennis"})

    def test_unknown_manual_series_does_not_enable_every_feed(self):
        self.assertEqual(categories_for_series({"KXUNKNOWN"}, {}, mode="manual"), set())


if __name__ == "__main__":
    unittest.main()
