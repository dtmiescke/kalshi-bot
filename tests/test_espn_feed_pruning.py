"""Known problem 7: ESPN feeds that always fail are not requested."""
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import sports_intel
from bot.storage import Store


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def scoreboard(event_id):
    return {"events": [{"id": event_id, "date": "2026-10-26T00:00Z", "name": "A vs B",
                        "status": {"type": {"state": "pre", "detail": "Sun"}},
                        "competitions": [{"competitors": [
                            {"team": {"displayName": "A", "abbreviation": "A"}, "homeAway": "home"},
                            {"team": {"displayName": "B", "abbreviation": "B"}, "homeAway": "away"}]}]}]}


class TestEspnFeedPruning(unittest.TestCase):
    def test_dead_scoreboards_removed(self):
        for key in ("motorsports_nascar", "cricket_ipl", "rugby_union"):
            self.assertNotIn(key, sports_intel.ESPN_SCOREBOARDS)
        for keys in sports_intel.SCOREBOARD_CATEGORIES.values():
            self.assertTrue(keys <= set(sports_intel.ESPN_SCOREBOARDS), keys)
        # working feeds stay
        for key in ("mma_ufc", "motorsports_f1", "golf_pga", "nfl"):
            self.assertIn(key, sports_intel.ESPN_SCOREBOARDS)

    def test_no_summary_requests_for_sports_without_event_summaries(self):
        urls = []

        def fake_request(url):
            urls.append(url)
            if "/scoreboard" in url:
                return FakeResponse(scoreboard("E1"))
            if "/summary" in url:
                return FakeResponse({})
            return FakeResponse({"rss": ""})

        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, {"SPORTS_INTEL_MAX_BOX_SCORES": "6"}), \
                patch.object(sports_intel, "_request", side_effect=fake_request), \
                patch.object(sports_intel, "_parse_rss", return_value=[]):
            st = Store(os.path.join(d, "t.db"))
            sports_intel.collect(st, force=True, categories={"mma", "basketball"})
            st.db.close()
        self.assertTrue(any("mma/ufc/scoreboard" in u for u in urls))        # scoreboard kept
        self.assertFalse(any("mma/ufc/summary" in u for u in urls))          # no dead summary
        self.assertTrue(any("basketball/nba/summary" in u for u in urls))    # others unchanged


if __name__ == "__main__":
    unittest.main()
