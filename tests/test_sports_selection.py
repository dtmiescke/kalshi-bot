import os
import unittest
from unittest.mock import patch

from bot.config import Settings
from bot.engine import _rotating_event_batch, _series_sport, _sports_excluded


class TestSportsSelection(unittest.TestCase):
    def test_configured_sports_are_default_and_combos_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        self.assertEqual(settings.sports_mode, "configured")
        self.assertEqual(settings.selected_series(), {
            "KXNFLGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME", "KXNCAAFGAME",
            "KXEPLGAME", "KXLALIGAGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME",
            "KXLIGUE1GAME", "KXUCLGAME", "KXUELGAME", "KXUECLGAME",
            "KXEREDIVISIEGAME", "KXLIGAMXGAME", "KXBRASILEIROGAME", "KXSAUDIPLGAME"})
        self.assertFalse(settings.combos_enabled)
        self.assertTrue(settings.combo_research_enabled)
        self.assertEqual(settings.combo_min_legs, 3)
        self.assertEqual(settings.combo_max_legs, 8)

    def test_all_sports_mode_remains_an_explicit_opt_in(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "all"}, clear=True):
            settings = Settings()
        self.assertEqual(settings.sports_mode, "all")
        self.assertIsNone(settings.selected_series())

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

    def test_fallback_routing_separates_nfl_and_college_football(self):
        self.assertEqual(_series_sport("UNKNOWN", {"title": "NFL matchup"}),
                         "americanfootball_nfl")
        self.assertEqual(_series_sport("UNKNOWN", {"title": "NCAA football matchup"}),
                         "americanfootball_ncaaf")
        self.assertEqual(_series_sport("UNKNOWN", {"title": "football championship"}),
                         "football")

    def test_manual_selection_keeps_nontraditional_sports(self):
        with patch.dict(os.environ, {"SPORTS_MODE": "manual",
                                    "SPORTS_SELECTED": "KXATPMATCH,KXF1RACE,KXESPORTS"}):
            settings = Settings()
        selected = settings.selected_series()
        series_map = {series: _series_sport(series) for series in selected}
        self.assertEqual(set(series_map), {"KXATPMATCH", "KXF1RACE", "KXESPORTS"})

    def test_rotating_event_batch_covers_all_events_without_starvation(self):
        events = [{"event_ticker": f"KXTEST-{n:03d}", "markets": []} for n in range(7)]
        batch1, cursor1 = _rotating_event_batch(events, "", budget=3)
        batch2, cursor2 = _rotating_event_batch(events, cursor1, budget=3)
        batch3, cursor3 = _rotating_event_batch(events, cursor2, budget=3)
        seen = {e["event_ticker"] for e in batch1 + batch2 + batch3}
        self.assertEqual(seen, {e["event_ticker"] for e in events})
        self.assertNotEqual(cursor1, cursor2)
        self.assertNotEqual(cursor2, cursor3)

    def test_rotating_event_batch_always_includes_held_events_and_deduplicates(self):
        events = [
            {"event_ticker": "KXTEST-A"},
            {"event_ticker": "KXTEST-B"},
            {"event_ticker": "KXTEST-B"},
            {"event_ticker": "KXTEST-C"},
        ]
        batch, cursor = _rotating_event_batch(events, "", budget=2, held={"KXTEST-C"})
        tickers = [e["event_ticker"] for e in batch]
        self.assertEqual(tickers.count("KXTEST-B"), 1)
        self.assertIn("KXTEST-C", tickers)
        self.assertEqual(cursor, "KXTEST-B")

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
