import os
import tempfile
import unittest

from bot import momentum, nba_tonight, nfl_tonight, shadow_monitor
from bot.storage import Store

TIP = "2030-01-01T00:00:00Z"


class TestFeatures(unittest.TestCase):
    def test_hot_cold_and_steady(self):
        season = [20.0] * 10
        hot = momentum.features(season, season[:-3] + [26.0, 25.0, 27.0], "pts")
        self.assertEqual(hot["trend"], "hot")
        self.assertGreater(hot["ratio"], 1.15)
        self.assertEqual(hot["last5"], [20.0, 20.0, 26.0, 25.0, 27.0])
        cold = momentum.features(season, season[:-3] + [10.0, 12.0, 9.0], "pts")
        self.assertEqual(cold["trend"], "cold")
        self.assertEqual(momentum.features(season, season, "pts")["trend"], "steady")

    def test_too_little_history_or_a_zero_average_gives_no_label(self):
        self.assertIsNone(momentum.features([1.0, 2.0], [1.0, 2.0]))
        self.assertIsNone(momentum.features([0.0] * 5, [0.0] * 5)["trend"])

    def test_minutes_trend(self):
        f = momentum.features([20.0] * 6, [20.0] * 6, "pts", [30.0] * 6, [34.0, 34.0, 34.0])
        self.assertEqual(f["min_trend"], 4.0)

    def test_alignment_with_the_pick_side(self):
        self.assertEqual(momentum.alignment("hot", "yes", ""), "with")          # a hot player on an over
        self.assertEqual(momentum.alignment("hot", "no", ""), "against")        # a hot player on an under
        self.assertEqual(momentum.alignment("cold", "no", ""), "with")
        self.assertEqual(momentum.alignment("cold", "yes", ""), "against")
        self.assertEqual(momentum.alignment("steady", "yes", ""), "steady")
        self.assertEqual(momentum.alignment(None, "no", ""), "steady")


def pick(ticker, label="A One 20+ points (YES)", side="yes", price=0.5, game="G", kind="prop"):
    return {"ticker": ticker, "game": game, "kind": kind, "label": label, "side": side, "price": price, "contracts": 10, "fee": 0.2,
            "p_model": 0.6, "edge": 0.1, "tip": TIP}


class TestStorageAndStudy(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def test_recording_a_pick_saves_its_momentum_once_per_sport(self):
        hot = momentum.features([20.0] * 8, [20.0] * 5 + [27.0] * 3, "pts")
        p = {**pick("T1"), "momentum": hot}
        self.assertEqual(nba_tonight.record_picks(self.store, {"picks": [p]}, now=1000), 1)
        self.assertEqual(momentum.load(self.store)["T1"]["trend"], "hot")
        q = {**pick("F1", "Q Back 60+ rushing yards (YES)"), "momentum": hot}
        self.assertEqual(nfl_tonight.record_picks(self.store, {"picks": [q]}, now=1000), 1)
        self.assertIn("F1", momentum.load(self.store))

    def settle(self, ticker, label, side, pnl, game, trend, price=0.5, table="nba_picks"):
        self.store.x(nba_tonight.SCHEMA)
        self.store.x(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'settled',NULL,?,5)",
                     (ticker, 1, game, "prop", label, side, price, 10, 5.0, .1, .6, .1, 2, pnl))
        momentum.save(self.store, ticker, "NBA", {"trend": trend}, 1)

    def test_study_groups_settled_picks_by_whether_they_go_with_momentum(self):
        for i in range(6):
            self.settle(f"W{i}", "X 20+ points (YES)", "yes", 4.0 if i % 3 else -5.0, f"G{i}", "hot")      # hot on an over: with
        for i in range(4):
            self.settle(f"A{i}", "Y 20+ points (YES)", "yes", -5.0, f"H{i}", "cold")                          # cold on an over: against
        self.settle("S1", "Z 20+ points (NO)", "no", 3.0, "K1", "steady")
        out = momentum.study(self.store)
        self.assertEqual((out["groups"]["with"]["n"], out["groups"]["against"]["n"], out["groups"]["steady"]["n"]), (6, 4, 1))
        self.assertEqual(out["groups"]["against"]["hit"], 0.0)
        self.assertGreater(out["groups"]["with"]["hit"], out["groups"]["against"]["hit"])
        self.assertEqual(out["verdict"], "collecting")                                                      # far fewer than the minimum
        self.assertEqual(out["tracked"], 11)

    def test_study_is_safe_on_an_empty_database(self):
        out = momentum.study(self.store)
        self.assertEqual(out["groups"]["with"], {"n": 0})
        self.assertEqual(out["verdict"], "collecting")

    def test_the_monitor_carries_momentum_for_picks_and_combo_legs(self):
        import json
        from bot import sports_combos
        self.store.x(nba_tonight.SCHEMA)
        self.store.x(sports_combos.SCHEMA)
        self.store.x("INSERT INTO nba_picks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',NULL,NULL,NULL)",
                     ("N1", 1, "A at B", "prop", "X 20+ points (YES)", "yes", .5, 10, 5.0, .1, .6, .1, 2000))
        momentum.save(self.store, "N1", "NBA", {"trend": "hot", "last3": 27.0, "season_avg": 20.0, "last5": [20.0] * 5, "unit": "pts"}, 1)
        legs = [{"ticker": "N1", "label": "X 20+ points (YES)", "side": "yes", "price": .5, "p_model": .6, "table": "nba_picks"}]
        self.store.x("INSERT INTO sports_combos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("K", 1, 1, json.dumps(legs), .25, .4, .3, .1, 20, 5.0, .1, 9, "open", None, None))
        out = shadow_monitor.report(self.store)
        self.assertEqual(out["picks"][0]["mom"]["trend"], "hot")
        self.assertEqual(out["combos"][0]["legs"][0]["mom"]["last3"], 27.0)


if __name__ == "__main__":
    unittest.main()
