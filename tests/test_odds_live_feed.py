"""Regression tests for explicit sportsbook-feed failures."""
import unittest
from unittest.mock import Mock, patch

from bot.odds import OddsClient
from bot.engine import Engine
from bot.config import Settings


class TestLiveSportsbookFeed(unittest.TestCase):
    def test_missing_api_key_is_not_misreported_as_empty_schedule(self):
        client = OddsClient("")
        with self.assertRaisesRegex(RuntimeError, "ODDS_API_KEY is missing"):
            client.fetch("basketball_nba")

    def test_unexpected_api_payload_is_rejected(self):
        client = OddsClient("test-key")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"message": "not an events list"}
        with patch("bot.odds.requests.get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "expected a list of events"):
                client.fetch("basketball_nba")

    def test_refresh_failure_preserves_cache_and_records_health(self):
        settings = Settings()
        store = Mock()
        store.get_kv.side_effect = lambda key, default=None: (
            [{"id": "old-event", "home_team": "Home", "away_team": "Away"}]
            if key == "odds_events_basketball_nba" else default
        )
        client = OddsClient("")
        engine = Engine(settings, store, odds_client=client)
        with patch("bot.engine.refresh_plan", return_value=(True, "test")), \
                patch.dict("os.environ", {"ESPN_ODDS": "no"}):      # the ESPN fallback has its own tests
            engine.refresh_odds("basketball_nba")
        calls = [call for call in store.set_kv.call_args_list
                 if call.args and call.args[0] == "odds_events_basketball_nba"]
        self.assertEqual(calls, [])
        health_calls = [call for call in store.set_kv.call_args_list
                        if call.args and call.args[0] == "odds_feed_health_basketball_nba"]
        self.assertEqual(len(health_calls), 1)
        self.assertEqual(health_calls[0].args[1]["status"], "error")
        self.assertEqual(health_calls[0].args[1]["cached_events"], 1)


if __name__ == "__main__":
    unittest.main()
