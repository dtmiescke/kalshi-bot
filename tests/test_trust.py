import os
import tempfile
import unittest

from bot import nba_tonight, nfl_tonight, trust
from bot.storage import Store

TIP = "2030-01-01T00:00:00Z"


def settled(store, table, i, p_model, price, won):
    store.x(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'settled',NULL,?,5)",
            (f"S{i}", 1, f"G{i % 8}", "prop", f"P{i} 5+ points (YES)", "yes", price, 10, 5.0, .1, p_model, .1, 2, 4.0 if won else -5.1))


class TestLearn(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.store.x(nba_tonight.SCHEMA)
        self.store.x(nfl_tonight.SCHEMA)

    def test_too_few_settled_picks_uses_the_cautious_default(self):
        for i in range(10):
            settled(self.store, "nba_picks", i, .8, .5, i % 2)
        out = trust.learn(self.store, "NBA")
        self.assertEqual((out["lam"], out["source"], out["n"]), (trust.DEFAULT_LAM, "default", 10))
        self.assertEqual(trust.learn(self.store, "NFL")["n"], 0)

    def test_a_model_that_tracks_the_price_and_overclaims_gets_no_trust(self):
        for i in range(80):                                                         # claims 80% on a 50% price, wins half the time
            settled(self.store, "nba_picks", i, .8, .5, i % 2)
        out = trust.learn(self.store, "NBA")
        self.assertEqual((out["source"], out["lam"]), ("learned", 0.0))

    def test_a_model_with_real_skill_earns_trust_up_to_the_cap(self):
        for i in range(80):                                                         # model says 80% or 20%, and it is right
            hi = i % 2 == 0
            settled(self.store, "nba_picks", i, .8 if hi else .2, .5, 1 if hi else 0)
        out = trust.learn(self.store, "NBA")
        self.assertEqual(out["lam"], trust.MAX_LAM)
        self.assertGreater(out["lam_hat"], trust.MAX_LAM)

    def test_judge_needs_the_trusted_chance_to_beat_price_plus_fee(self):
        self.assertEqual(trust.judge(.7, .5, .01, 0.0)[2], False)                   # no trust: nothing beats the price
        p_cal, edge, ok = trust.judge(.8, .5, .01, 0.5)
        self.assertEqual((p_cal, edge, ok), (0.65, 0.14, True))
        self.assertFalse(trust.judge(.56, .5, .01, 0.5)[2])                         # 0.53 - 0.5 - 0.01 = 2% < the 3% bar

    def test_recording_a_pick_saves_its_verdict_and_zero_trust_marks_it_research(self):
        for i in range(80):
            settled(self.store, "nba_picks", i, .8, .5, i % 2)
        pick = {"ticker": "N1", "game": "A at B", "kind": "prop", "label": "X 20+ points (YES)", "side": "yes", "price": .5,
                "contracts": 10, "fee": .2, "p_model": .8, "edge": .2, "tip": TIP}
        nba_tonight.record_picks(self.store, {"picks": [pick]}, now=1000)
        v = trust.load(self.store)["N1"]
        self.assertEqual((v["lam"], v["official"]), (0.0, False))
        q = {**pick, "ticker": "F1", "label": "Q Back 60+ rushing yards (YES)"}
        nfl_tonight.record_picks(self.store, {"picks": [q]}, now=1000)                # no NFL history: default trust
        self.assertEqual(trust.load(self.store)["F1"]["lam"], trust.DEFAULT_LAM)


if __name__ == "__main__":
    unittest.main()
