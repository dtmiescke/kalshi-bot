"""Known problem 8: Odds API refreshes fit the free monthly credits."""
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests
from datetime import datetime, timezone

from bot import odds
from bot.storage import Store

NOW = int(datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp())   # 7 days left in October


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestOddsBudget(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def plan(self, next_game, base=240):
        return odds.refresh_plan(self.st, "basketball_nba", NOW, next_game, base)

    def test_next_game_from_kalshi_market_fields(self):
        events = [{"markets": [{"status": "active", "occurrence_datetime": iso(NOW + 7200)},
                               {"status": "active", "expected_expiration_time": iso(NOW + 3600)}]},
                  {"markets": [{"status": "active", "occurrence_datetime": iso(NOW - 5 * 3600)}]},
                  {"markets": [{"status": "settled", "occurrence_datetime": iso(NOW + 60)}]}]
        self.assertEqual(odds.next_game_ts(events, NOW), NOW + 3600)
        self.assertIsNone(odds.next_game_ts([], NOW))
        self.assertEqual(odds.next_game_ts(
            [{"start_time": iso(NOW + 1800), "markets": []}], NOW), NOW + 1800)
        self.assertEqual(odds.next_game_ts(
            [{"markets": [{"status": "active", "close_time": iso(NOW + 2400)}]}], NOW),
            NOW + 2400)

    def test_no_game_soon_means_no_spend(self):
        self.assertEqual(self.plan(NOW + 48 * 3600), (False, "no game soon"))

    def test_four_hourly_far_from_game_hourly_near_it(self):
        self.st.set_kv("odds_last_basketball_nba", NOW - 2 * 3600)
        self.assertEqual(self.plan(NOW + 10 * 3600), (False, "fresh"))        # 2h < 4h
        self.assertEqual(self.plan(NOW + 2 * 3600), (True, "near game"))      # 2h > 1h
        self.assertEqual(self.plan(None), (False, "fresh"))                   # unknown: 4h rule
        # An empty cache must retry after a prior empty/failed response, even
        # inside the normal refresh interval; monthly/daily limits still apply.
        self.assertEqual(
            odds.refresh_plan(self.st, "basketball_nba", NOW, NOW + 2 * 3600,
                              240, cache_empty=True),
            (True, "near game"))
        self.assertEqual(
            odds.refresh_plan(self.st, "basketball_nba", NOW, None, 240,
                              cache_empty=True),
            (True, "scheduled"))

    def test_daily_pacing_and_hard_stop(self):
        self.st.set_kv("odds_quota", {"remaining": 70})                       # 70 / 7 days = 10 a day
        self.st.set_kv("odds_spent_20261025", 10)
        self.assertEqual(self.plan(NOW + 10 * 3600), (False, "daily budget used"))
        self.assertEqual(self.plan(NOW + 3600), (True, "near game"))          # near game: up to 2x
        self.st.set_kv("odds_spent_20261025", 20)
        self.assertEqual(self.plan(NOW + 3600), (False, "daily budget used"))
        self.st.set_kv("odds_quota", {"remaining": 10})
        self.assertEqual(self.plan(NOW + 3600), (False, "quota exhausted"))

    @patch("bot.odds.requests.get")
    def test_auth_and_invalid_sport_errors_raise_instead_of_looking_like_empty_odds(self, get):
        for status in (401, 422):
            response = Mock()
            response.status_code = status
            response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
            get.return_value = response
            with self.subTest(status=status):
                with self.assertRaises(requests.HTTPError):
                    odds.OddsClient("configured-key").fetch("basketball_nba")
        self.assertEqual(get.call_count, 2)

    def test_record_spend_tracks_provider_counts_and_alerts_once_at_80_percent(self):
        class Client:
            last_remaining, last_used = "90", "410"
        alerts = []
        odds.record_spend(self.st, Client(), NOW, alert=lambda *a, **k: alerts.append(a))
        odds.record_spend(self.st, Client(), NOW + 60, alert=lambda *a, **k: alerts.append(a))
        self.assertEqual(self.st.get_kv("odds_spent_20261025"), 2)
        self.assertEqual(self.st.get_kv("odds_quota")["remaining"], 90)
        self.assertEqual(len(alerts), 1)


if __name__ == "__main__":
    unittest.main()
