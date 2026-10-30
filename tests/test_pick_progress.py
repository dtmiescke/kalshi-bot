import os
import tempfile
import unittest
from unittest.mock import MagicMock

from bot import nba_tonight, nfl_tonight, pick_progress as pp, shadow_monitor
from bot.storage import Store


def nba_box(players):
    return {"boxscore": {"players": [{"team": {"displayName": "T"}, "statistics": [
        {"labels": ["MIN", "PTS", "FG", "3PT", "REB", "AST", "STL", "BLK"],
         "athletes": [{"athlete": {"displayName": n}, "stats": s} for n, s in players]}]}]}}


class TestParsing(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(pp.parse_label("Jayson Tatum 25+ points (NO)"), ("Jayson Tatum", 25, "points", "no"))
        self.assertEqual(pp.parse_label("Franz Wagner 35+ pts+reb+ast (NO)"), ("Franz Wagner", 35, "pts+reb+ast", "no"))
        self.assertIsNone(pp.parse_label("Boston Celtics to win"))

    def test_nba_values_sum_the_right_columns_and_read_made_threes(self):
        box = nba_box([("Ja Morant", ["23", "14", "5-12", "2-5", "2", "3", "1", "0"])])
        self.assertEqual(pp.read_box("NBA", box, "Ja Morant", "points"), (14.0, 23.0))
        self.assertEqual(pp.read_box("NBA", box, "Ja Morant", "pts+reb+ast"), (19.0, 23.0))
        self.assertEqual(pp.read_box("NBA", box, "Ja Morant", "threes"), (2.0, 23.0))
        self.assertIsNone(pp.read_box("NBA", box, "Somebody Else", "points"))
        self.assertIsNone(pp.read_box("NBA", nba_box([("Ja Morant", [])]), "Ja Morant", "points"))        # on the roster, has not played

    def test_nfl_reads_the_matching_category_only(self):
        box = {"boxscore": {"players": [{"statistics": [
            {"name": "rushing", "labels": ["CAR", "YDS"], "athletes": [{"athlete": {"displayName": "Josh Allen"}, "stats": ["6", "41"]}]},
            {"name": "passing", "labels": ["C/ATT", "YDS", "TD"], "athletes": [{"athlete": {"displayName": "Josh Allen"}, "stats": ["20/30", "255", "2"]}]}]}]}}
        self.assertEqual(pp.read_box("NFL", box, "Josh Allen", "passing yards"), (255.0, None))
        self.assertEqual(pp.read_box("NFL", box, "Josh Allen", "rushing yards"), (41.0, None))
        self.assertEqual(pp.read_box("NFL", box, "Josh Allen", "passing TDs"), (2.0, None))


EVENT = {"id": "9", "status": {"type": {"state": "in", "shortDetail": "6:12 - 3rd"}},
         "competitions": [{"competitors": [{"homeAway": "home", "score": "71", "team": {"displayName": "Dallas Mavericks", "abbreviation": "DAL"}},
                                          {"homeAway": "away", "score": "74", "team": {"displayName": "Houston Rockets", "abbreviation": "HOU"}}]}]}


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.store.x(nba_tonight.SCHEMA)
        self.store.x(nfl_tonight.SCHEMA)
        self.store.x("INSERT INTO nba_picks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("T1", 1, "Houston Rockets at Dallas Mavericks", "prop", "Ja Morant 25+ points (NO)", "no", .58, 8, 4.64, .14, .73, .13, 1000,
                      "open", None, None, None))

    def fetch(self, started=True):
        def f(url, params=None, timeout=None):
            r = MagicMock(status_code=200)
            if url.endswith("/scoreboard"):
                r.json.return_value = {"events": [EVENT]}
            else:
                r.json.return_value = nba_box([("Ja Morant", ["24", "12", "", "", "", "", "", ""])])
            return r
        return f

    def test_a_started_game_gives_current_stats_and_the_game_state(self):
        self.assertEqual(pp.refresh(self.store, now=2000, every=0, fetch=self.fetch()), 1)
        got = self.store.get_kv("pick_progress")["T1"]
        self.assertEqual((got["cur"], got["line"], got["min"], got["state"]), (12.0, 25, 24.0, "in"))
        self.assertEqual((got["clock"], got["score"]), ("6:12 - 3rd", "HOU 74 - DAL 71"))

    def test_games_that_have_not_started_make_no_calls(self):
        f = MagicMock()
        self.assertEqual(pp.refresh(self.store, now=500, every=0, fetch=f), 0)             # tip is 1000
        self.assertEqual(f.call_count, 0)
        self.assertIsNone(pp.refresh(self.store, now=501, every=600, fetch=f))             # throttled

    def test_the_monitor_carries_the_progress_for_picks(self):
        pp.refresh(self.store, now=2000, every=0, fetch=self.fetch())
        row = shadow_monitor.report(self.store)["picks"][0]
        self.assertEqual(row["prog"]["cur"], 12.0)


if __name__ == "__main__":
    unittest.main()
