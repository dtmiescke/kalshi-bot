import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from bot.sports_intel import _football_matchup_profile, _parse_espn, _parse_rss, _request, ensure_schema, recent, _record_items
from bot.storage import Store


class TestSportsIntelligence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.sqlite"))
        ensure_schema(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    @patch("bot.sports_intel.requests.get")
    def test_espn_request_falls_back_when_primary_host_is_forbidden(self, get):
        forbidden = Mock()
        forbidden.raise_for_status.side_effect = __import__("requests").HTTPError("403 Forbidden")
        ok = Mock()
        ok.content = b'{"events":[]}'
        ok.raise_for_status.return_value = None
        get.side_effect = [forbidden, ok]

        response = _request("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard")

        self.assertIs(response, ok)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            get.call_args_list[1].args[0],
            "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        )
        self.assertIn("Referer", get.call_args_list[1].kwargs["headers"])

    def test_rss_parses_and_deduplicates_stable_ids(self):
        xml = b"""<?xml version="1.0"?><rss><channel><item><title>Star player ruled out</title><link>https://example.test/story</link><description>Official update</description><pubDate>Wed, 21 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
        first = _parse_rss(xml, "fixture", "basketball", 1792584000)
        second = _parse_rss(xml, "fixture", "basketball", 1792584000)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(first[0]["kind"], "news")
        self.assertEqual(first[0]["title"], "Star player ruled out")
        self.assertIsNotNone(first[0]["published_ts"])

    def test_scoreboard_parses_live_score_state(self):
        data = {"events": [{"id": "123", "name": "Home vs Away", "date": "2026-10-21T19:00Z",
            "status": {"displayClock": "02:10", "period": 4, "type": {"state": "in", "description": "In Progress"}},
            "competitions": [{"competitors": [
                {"homeAway": "home", "score": "98", "team": {"displayName": "Home"}},
                {"homeAway": "away", "score": "94", "team": {"displayName": "Away"}}]}]}]}
        events = _parse_espn(data, "fixture", "nba", 1792600000)
        self.assertEqual(events[0]["state"], "in")
        self.assertEqual(events[0]["competitors"][0]["score"], "98")
        self.assertFalse(events[0]["verified"])

    def test_football_profile_extracts_roles_and_keeps_missing_fields_unknown(self):
        event = {
            "provider_event_id": "nfl-123", "name": "Away at Home",
            "competitors": [
                {"name": "Home Team", "abbreviation": "HOM", "home_away": "home", "score": "21"},
                {"name": "Away Team", "abbreviation": "AWY", "home_away": "away", "score": "17"},
            ],
        }
        box = {
            "teams": [{"team": {"displayName": "Home Team"}, "statistics": [
                {"name": "totalYards", "displayValue": "350"},
                {"name": "rushingYards", "displayValue": "105"},
                {"name": "turnovers", "displayValue": "1"},
            ]}],
            "players": [{"team": {"displayName": "Home Team"}, "statistics": [{
                "labels": ["CMP/ATT", "YDS", "TD", "INT"],
                "athletes": [{"athlete": {"displayName": "Example QB",
                                           "position": {"abbreviation": "QB"}},
                              "starter": True, "stats": ["20/30", "245", "2", "0"]}],
            }]}],
        }
        profile = _football_matchup_profile(event, box, {}, 1792600000, "nfl")
        self.assertEqual(profile["sport_profile"], "american_football")
        self.assertEqual(profile["league_domain"], "nfl")
        self.assertEqual(profile["competition_level"], "professional")
        self.assertEqual(profile["learning_partition"], "sports:nfl")
        self.assertEqual(profile["event_id"], "nfl-123")
        college_profile = _football_matchup_profile(event, box, {}, 1792600000, "college_football")
        self.assertEqual(college_profile["league_domain"], "ncaa_football")
        self.assertEqual(college_profile["competition_level"], "college")
        self.assertEqual(college_profile["learning_partition"], "sports:ncaa_football")
        self.assertEqual(profile["quarterbacks"][0]["name"], "Example QB")
        self.assertEqual(profile["quarterbacks"][0]["statistics"]["YDS"], "245")
        self.assertTrue(profile["data_quality"]["team_stats_available"])
        self.assertIn("turnovers", profile["team_statistics"][0]["key_metrics"])
        self.assertIn("rushing_yards", profile["team_statistics"][0]["key_metrics"])
        self.assertTrue(profile["data_quality"]["player_stats_available"])
        self.assertFalse(profile["data_quality"]["injury_data_available"])
        self.assertFalse(profile["lineups_confirmed"])
        self.assertIn("confirmed_starting_lineups", profile["missing_information"])

    def test_store_deduplicates_repeated_items(self):
        item = {"id": "same", "kind": "news", "category": "football", "source": "fixture",
                "title": "Headline", "url": "https://example.test", "summary": "Summary",
                "published_ts": None, "observed_ts": 100, "confidence": 0.3}
        self.assertEqual(_record_items(self.store, [item]), 1)
        self.assertEqual(_record_items(self.store, [item]), 0)
        self.assertEqual(len(recent(self.store, kind="news")), 1)
        payload = json.loads(self.store.q("SELECT payload_json FROM sports_intel_items")[0]["payload_json"])
        self.assertEqual(payload["title"], "Headline")


if __name__ == "__main__":
    unittest.main()
