import os
import random
import tempfile
import unittest

from bot import experiment, nba_tonight, nfl_tonight, scorecard, sports_combos
from bot.storage import Store


def put_bet(store, series, event, pnl, stake=10.0, ts=1_000_000, mode="paper"):
    store.x("""INSERT INTO bets (ts, mode, ticker, event_ticker, series, team, side, price, contracts, stake, fee,
               model_prob, edge, confidence, signals_json, order_id, status, pnl, graded_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, mode, f"{event}-{pnl}", event, series, "x", "yes", .5, 20, stake, .1, .6, .1, .5, "{}", None, "graded", pnl, ts))


class TestVerdicts(unittest.TestCase):
    def test_minimums_come_first(self):
        self.assertEqual(scorecard.verdict(50, 50, .1, .2), "too_early")
        self.assertEqual(scorecard.verdict(500, 5, .1, .2), "too_early")
        self.assertEqual(scorecard.verdict(500, 50, None, None), "too_early")

    def test_proven_losing_and_not_proven(self):
        self.assertEqual(scorecard.verdict(500, 50, .02, .20), "proven")
        self.assertEqual(scorecard.verdict(500, 50, .02, .20, ex_top=120.0), "proven")
        self.assertEqual(scorecard.verdict(500, 50, .02, .20, ex_top=-40.0), "fragile")      # carried by its best wins
        self.assertEqual(scorecard.verdict(500, 50, -.30, -.05), "losing")
        self.assertEqual(scorecard.verdict(500, 50, -.10, .10), "not_proven")


class TestInterval(unittest.TestCase):
    def test_a_clear_edge_has_an_interval_above_zero_and_noise_straddles_it(self):
        rng = random.Random(1)
        win = [(i, 10.0, 10.0 * (0.3 + rng.uniform(-.2, .2))) for i in range(60)]
        low, high = scorecard._ci(win)
        self.assertGreater(low, 0)
        noise = [(i, 10.0, rng.choice([-10.0, 9.0])) for i in range(60)]
        low, high = scorecard._ci(noise)
        self.assertLess(low, 0 < high or 0.001)

    def test_correlated_picks_widen_the_interval(self):
        solo = [(i, 10.0, (5.0 if i % 2 else -5.0)) for i in range(40)]            # 40 independent clusters
        lumped = [(i // 20, 10.0, (5.0 if i < 20 else -5.0)) for i in range(40)]   # same dollars, but two games that each swing together
        a, b = scorecard._ci(solo)
        c, d = scorecard._ci(lumped)
        self.assertGreaterEqual(d - c, b - a)

    def test_too_few_clusters_has_no_interval_and_is_deterministic(self):
        self.assertEqual(scorecard._ci([(1, 10.0, 5.0)]), (None, None))
        rows = [(i, 10.0, float(i % 7) - 3) for i in range(30)]
        self.assertEqual(scorecard._ci(rows), scorecard._ci(rows))


class TestLane(unittest.TestCase):
    def test_dollars_per_hundred_and_brier_skill(self):
        rows = [(i, 10.0, 10.0 if i % 2 else -10.0) for i in range(10)]
        probs = [(0.9, 0.5, 1.0 if i % 2 else 0.0) for i in range(10)]           # claims 90% but hits 50%
        out = scorecard.lane("x", rows, probs)
        self.assertEqual((out["n"], out["wins"], out["dollars"], out["per100"]), (10, 5, 0.0, 0.0))
        self.assertEqual((out["forecast"], out["hit"]), (90.0, 50.0))
        self.assertLess(out["skill"], 0)                                           # worse than believing the price
        self.assertEqual(out["verdict"], "too_early")
        good = scorecard.lane("y", rows, [(0.5 + (0.3 if i % 2 else -0.3), 0.5, 1.0 if i % 2 else 0.0) for i in range(10)])
        self.assertGreater(good["skill"], 0)


class TestFragile(unittest.TestCase):
    def test_a_lane_carried_by_a_few_big_wins_is_fragile_not_proven(self):
        rows = [(i, 10.0, -1.0) for i in range(150)] + [(1000 + i, 10.0, 60.0) for i in range(10)]
        out = scorecard.lane("lucky", rows)
        self.assertGreater(out["dollars"], 0)
        self.assertLess(out["ex_top"], 0)                                          # -150 once the ten big wins are removed
        self.assertNotEqual(out["verdict"], "proven")
        steady = scorecard.lane("steady", [(i, 10.0, 2.0 if i % 5 else -3.0) for i in range(200)])
        self.assertEqual(steady["verdict"], "proven")
        self.assertGreater(steady["ex_top"], 0)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def test_empty_database_is_safe(self):
        out = scorecard.report(self.store)
        self.assertEqual((out["lanes"], out["proven"]), ([], []))

    def test_each_lane_gets_its_own_bets_and_other_modes_are_ignored(self):
        put_bet(self.store, "KXBTC15M", "E1", 5.0)
        put_bet(self.store, "KXBTC15M", "E2", -10.0)
        put_bet(self.store, "KXBTCD", "L1", -20.0)
        put_bet(self.store, "KXNBAGAME", "S1", 3.0)
        put_bet(self.store, "KXBTC15M", "E3", 99.0, mode="live")
        out = {x["lane"]: x for x in scorecard.report(self.store)["lanes"]}
        self.assertEqual(out["15-minute crypto"]["n"], 2)
        self.assertEqual(out["15-minute crypto"]["dollars"], -5.0)
        self.assertEqual(out["Hourly and daily ladders"]["dollars"], -20.0)
        self.assertEqual(out["Sportsbook-based bets"]["dollars"], 3.0)

    def test_nba_and_experiment_lanes_read_their_tables(self):
        self.store.x(nba_tonight.SCHEMA)
        self.store.x(nfl_tonight.SCHEMA)
        self.store.x(sports_combos.SCHEMA)
        experiment.ensure_table(self.store)
        for i, (kind, pnl) in enumerate((("prop", 5.0), ("prop", -5.0), ("ml", 2.0))):
            self.store.x("""INSERT INTO nba_picks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (f"T{i}", 1, "G", kind, "l", "yes", .5, 10, 5.0, .1, .6, .1, 2, "settled", "yes", pnl, 3))
        self.store.x("INSERT INTO experiment_bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("X1", 3600, "KXBTC15M", "yes", .5, 10, 5.0, .1, .6, .05, 8, "settled", 1, 4.0, 4000))
        out = {x["lane"]: x for x in scorecard.report(self.store)["lanes"]}
        self.assertEqual(out["NBA player props (shadow)"]["n"], 2)
        self.assertEqual(out["NBA moneylines (shadow)"]["dollars"], 2.0)
        self.assertEqual(out["NBA player props (shadow)"]["forecast"], 60.0)
        self.assertEqual(out["Loosened-rules experiment (shadow)"]["wins"], 1)
        self.store.x("INSERT INTO nfl_picks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("N1", 1, "A at B", "prop", "l", "no", .4, 12, 4.8, .1, .7, .2, 2, "settled", "no", 7.0, 3))
        self.assertEqual({x["lane"]: x for x in scorecard.report(self.store)["lanes"]}["NFL player props (shadow)"]["dollars"], 7.0)
        self.store.x("INSERT INTO sports_combos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("K1", 86400 * 5, 3, "[]", .2, .5, .35, .1, 20, 4.0, .1, 1, "settled", -4.1, 2))
        self.assertEqual({x["lane"]: x for x in scorecard.report(self.store)["lanes"]}["Bet combos, several players (shadow)"]["dollars"], -4.1)


if __name__ == "__main__":
    unittest.main()
