import json
import os
import tempfile
import unittest

from bot.sports_intel import _parse_espn, _parse_rss, ensure_schema, recent, _record_items
from bot.storage import Store


class TestSportsIntelligence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.sqlite"))
        ensure_schema(self.store)

    def tearDown(self):
        self.tmp.cleanup()

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
