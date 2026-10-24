"""Tests for the sports candidate counsel gate."""
import os
import tempfile
import unittest

from bot.storage import Store
from bot.signals.base import Reading
from bot.sports_counsel import review


class TestSportsCounsel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "bot.sqlite"))
        self.old_mode = os.environ.get("COUNCIL_MODE")
        os.environ["COUNCIL_MODE"] = "off"

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
        if self.old_mode is None:
            os.environ.pop("COUNCIL_MODE", None)
        else:
            os.environ["COUNCIL_MODE"] = self.old_mode

    def test_numeric_candidate_passes_when_counsel_disabled(self):
        readings = [Reading("book_consensus", .62, .8, {"age_min": 5, "n_books": 4})]
        result = review(None, self.store,
                        {"event_ticker": "EVT", "title": "Team A vs Team B"},
                        {"ticker": "MKT", "series": "KXNFLGAME", "event_ticker": "EVT"},
                        {"side": "yes", "price": .5, "edge": .12},
                        readings, .62, .8, now=1000)
        self.assertEqual(result["decision"], "approve")

    def test_missing_sportsbook_consensus_is_rejected(self):
        readings = [Reading("market", .62, .8, {})]
        result = review(None, self.store, {"event_ticker": "EVT"},
                        {"ticker": "MKT", "series": "KXNFLGAME"},
                        {"side": "yes", "price": .5, "edge": .12},
                        readings, .62, .8, now=1000)
        self.assertEqual(result["decision"], "reject")

    def test_invalid_probability_is_rejected(self):
        readings = [Reading("book_consensus", .62, .8, {"age_min": 5})]
        result = review(None, self.store, {"event_ticker": "EVT"},
                        {"ticker": "MKT", "series": "KXNFLGAME"},
                        {"side": "yes", "price": .5, "edge": .12},
                        readings, 1.5, .8, now=1000)
        self.assertEqual(result["decision"], "reject")


if __name__ == "__main__":
    unittest.main()
