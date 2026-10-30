import json
import os
import tempfile
import unittest

from bot import nba_tonight, nfl_tonight, shadow_monitor, sports_combos
from bot.storage import Store


def pick(store, table, ticker, label, status="open", pnl=None, kind="prop", edge=0.1, tip=2000, settled=None):
    store.x(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, 1, "A at B", kind, label, "yes", .5, 10, 5.0, .1, .6, edge, tip, status, None, pnl, settled))


class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def test_empty_database_is_safe(self):
        out = shadow_monitor.report(self.store)
        self.assertEqual((out["picks"], out["combos"]), ([], []))
        self.assertEqual(out["lanes"]["NBA props"]["picks"], 0)

    def test_lanes_picks_and_combo_legs_are_joined(self):
        self.store.x(nba_tonight.SCHEMA)
        self.store.x(nfl_tonight.SCHEMA)
        self.store.x(sports_combos.SCHEMA)
        pick(self.store, "nba_picks", "N1", "Ja Morant 6+ assists (YES)", "settled", 4.0, settled=5)
        pick(self.store, "nba_picks", "N2", "Ty Jerome 3+ threes (YES)", "settled", -5.2, settled=6)
        pick(self.store, "nba_picks", "N3", "Boston Celtics to win", kind="ml", edge=0.2)
        pick(self.store, "nfl_picks", "F1", "Jared Goff 275+ passing yards (YES)", edge=0.3, tip=1000)
        legs = [{"ticker": "N1", "label": "Ja Morant 6+ assists (YES)", "price": .5, "table": "nba_picks"},
                {"ticker": "F1", "label": "Jared Goff 275+ passing yards (YES)", "price": .4, "table": "nfl_picks"}]
        self.store.x("INSERT INTO sports_combos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("K", 1, 2, json.dumps(legs), .22, .5, .4, .1, 20, 4.4, .1, 9, "open", None, None))
        out = shadow_monitor.report(self.store)
        self.assertEqual(out["lanes"]["NBA props"]["wins"], 1)
        self.assertEqual(out["lanes"]["NBA props"]["pnl"], -1.2)
        self.assertEqual(out["lanes"]["NBA game winners"]["open"], 1)
        self.assertEqual(out["lanes"]["NFL props"]["picks"], 1)
        self.assertEqual(out["lanes"]["Bet combos"]["open"], 1)
        combo = out["combos"][0]
        self.assertEqual([g["status"] for g in combo["legs"]], ["won", "open"])                 # leg statuses come from the picks
        self.assertEqual([(g["id"], g["game"], g["side"]) for g in combo["legs"]], [("N1", "A at B", "yes"), ("F1", "A at B", "yes")])   # each leg has its own details
        sports = [(p["sport"], p["label"], p["status"]) for p in out["picks"]]
        self.assertEqual(sports[0][0], "NBA")                                                    # open picks first within a sport
        self.assertIn(("NFL", "Jared Goff 275+ passing yards (YES)", "open"), sports)

    def test_lists_are_capped(self):
        self.store.x(nba_tonight.SCHEMA)
        for i in range(shadow_monitor.OPEN_CAP + 30):
            pick(self.store, "nba_picks", f"T{i}", f"Player {i} 5+ points (YES)", tip=i)
        out = shadow_monitor.report(self.store)
        self.assertEqual(len(out["picks"]), shadow_monitor.OPEN_CAP)
        self.assertEqual(out["lanes"]["NBA props"]["picks"], shadow_monitor.OPEN_CAP + 30)       # totals still count everything


if __name__ == "__main__":
    unittest.main()
