"""Go-live checklist: seven fixed checks, all must pass; a few big wins cannot fake a pass."""
import os
import random
import tempfile
import unittest

from bot import readiness
from bot.storage import Store


class TestReadiness(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.rng = random.Random(3)
        self.n = 0

    def bet(self, series, stake, pnl):
        self.n += 1
        self.store.q("""INSERT INTO bets (ts, mode, ticker, event_ticker, series, side, price, contracts, stake, fee, status, pnl)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (1_790_000_000 + self.n, "paper", f"{series}-{self.n}", f"{series}-{self.n}", series, "yes", .5, 10, stake, 0.1, "graded", pnl))

    def by_id(self, rep):
        return {x["id"]: x for x in rep["criteria"]}

    def test_empty_store_fails_every_check(self):
        rep = readiness.report(self.store, {})
        self.assertFalse(rep["ready"])
        self.assertEqual(rep["total"], 7)
        self.assertEqual(rep["met"], 0)                              # no data is not "no alarm"

    def test_a_few_big_wins_do_not_pass_the_per_dollar_or_robustness_checks(self):
        for _ in range(230):
            self.bet("KXBTC15M", 20.0, 20.0 if self.rng.random() < 0.45 else -20.0)       # small bets, a slightly losing coin flip
        for _ in range(9):
            self.bet("KXBTC15M", 700.0, 500.0)                      # a handful of large winners
        rep = readiness.report(self.store, {})
        c = self.by_id(rep)
        total = sum(r["pnl"] for r in self.store.q("SELECT pnl FROM bets"))
        self.assertGreater(total, 0)                                # the dollar total looks great...
        self.assertFalse(c["robust"]["met"])                        # ...but not without the best 10 bets
        self.assertFalse(c["per_dollar"]["met"])
        self.assertFalse(c["bets"]["met"])

    def test_a_real_steady_edge_on_two_coins_passes_the_bet_based_checks(self):
        for series in ("KXBTC15M", "KXETH15M"):
            for _ in range(600):
                win = self.rng.random() < 0.6                      # wins pay $1 per $1 at even odds: +20% per dollar
                self.bet(series, 10.0, 10.0 if win else -10.0)
        c = self.by_id(readiness.report(self.store, {}))
        for k in ("bets", "per_dollar", "robust", "coins"):
            self.assertTrue(c[k]["met"], (k, c[k]))

    def test_summary_driven_checks(self):
        summary = {"online": {"bot": {"z": 2.5, "drift": {"alarm": False}}},
                   "sweep": {"verdict": "holds up"},
                   "entry_rules": {"rules": [{"label": "X", "state": "paying"}]},
                   "slices": {"slices": [{"dim": "coin", "label": "BTC", "state": "paying"},
                                         {"dim": "all bets", "label": "all bets", "state": "paying"}]}}
        c = self.by_id(readiness.report(self.store, summary))
        self.assertTrue(c["beats"]["met"])
        self.assertTrue(c["study"]["met"])
        self.assertTrue(c["drift"]["met"])
        self.assertIn("settings search", c["study"]["value"])
        self.assertNotIn("all bets", c["study"]["value"])           # the overall row never counts as a finding
        bad = self.by_id(readiness.report(self.store, {"online": {"bot": {"z": -2.4, "drift": {"alarm": True}}}}))
        self.assertFalse(bad["beats"]["met"])
        self.assertFalse(bad["drift"]["met"])
        self.assertFalse(bad["study"]["met"])

    def test_sports_and_other_series_do_not_count(self):
        for _ in range(5):
            self.bet("KXMLBGAME", 10.0, 10.0)
        self.assertEqual(readiness.report(self.store, {})["bets"], 0)


if __name__ == "__main__":
    unittest.main()
