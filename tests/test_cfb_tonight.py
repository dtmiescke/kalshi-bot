import json
import os
import tempfile
import unittest

from bot import cfb_tonight as cfb
from bot.storage import Store

NOW = 1_793_500_000
TIP = "2026-11-01T00:00:00Z"


def espn_game(i, home, away, ml_home, ml_away, state="pre"):
    return {"id": str(i), "date": TIP, "status": {"type": {"state": state}},
            "competitions": [{"competitors": [{"homeAway": "home", "team": {"displayName": home}},
                                              {"homeAway": "away", "team": {"displayName": away}}],
                              "odds": [{"moneyline": {"home": {"close": {"odds": ml_home}}, "away": {"close": {"odds": ml_away}}}}]}]}


class Resp:
    def __init__(self, events):
        self.events = events

    def raise_for_status(self):
        pass

    def json(self):
        return {"events": self.events}


def kev(home, away, fav, bid, ask):
    other = away if fav == home else home
    return {"title": f"{away} vs {home}", "markets": [
        {"ticker": f"K-{fav[:3]}", "yes_sub_title": fav, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask)},
        {"ticker": f"K-{other[:3]}", "yes_sub_title": other, "yes_bid_dollars": str(round(1 - ask, 2)), "yes_ask_dollars": str(round(1 - bid, 2))}]}


class TestBook(unittest.TestCase):
    def test_devigged_chances_sum_to_one_and_started_games_are_skipped(self):
        games = [espn_game(1, "Texas Tech", "Arizona", "-180", "+150"), espn_game(2, "Iowa", "Wisconsin", "-135", "+114", state="in")]
        out = cfb.book_games(lambda *a, **k: Resp(games), now=NOW)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["p_home"] + out[0]["p_away"], 1.0)
        self.assertGreater(out[0]["p_home"], 0.5)

    def test_title_matching_and_favourite_pick(self):
        games = [{"id": "1", "home": "Texas Tech Red Raiders", "away": "Arizona Wildcats", "tip": TIP, "p_home": .62, "p_away": .38}]
        c = cfb.candidates([kev("Texas Tech", "Arizona", "Texas Tech", .61, .63)], games)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["team"], "Texas Tech")
        self.assertAlmostEqual(c[0]["price"], .63)
        self.assertLess(c[0]["edge"], 0)                       # a fee on a fair price: shown honestly, not hidden
        self.assertEqual(cfb.candidates([kev("Texas Tech", "Arizona", "Texas Tech", .61, .63)], []), [])

    def test_wide_spreads_and_extreme_prices_are_dropped(self):
        games = [{"id": "1", "home": "Texas Tech Red Raiders", "away": "Arizona Wildcats", "tip": TIP, "p_home": .95, "p_away": .05}]
        self.assertEqual(cfb.candidates([kev("Texas Tech", "Arizona", "Texas Tech", .90, .99)], games), [])
        self.assertEqual(cfb.candidates([kev("Texas Tech", "Arizona", "Texas Tech", .80, .92)], games), [])


def cand(i, price, p=None):
    return {"ticker": f"T{i}", "team": f"Team{i}", "game": f"A{i} at B{i}", "gid": str(i), "tip": TIP, "price": price,
            "p_book": p or price, "contracts": 5, "fee": .1, "edge": 0.0}


class TestCombos(unittest.TestCase):
    def test_one_combo_per_tier_with_distinct_games_and_product_price(self):
        cands = [cand(i, p) for i, p in enumerate([.85, .82, .80, .78, .75, .72, .68, .66, .62, .60, .58, .55])]
        combos = cfb.build_combos(cands)
        self.assertEqual([c["tier"] for c in combos], ["Chalk", "Middle", "Riskier"])
        for c in combos:
            self.assertEqual(len({l["gid"] for l in c["legs"]}), cfb.LEGS)
            want = 1.0
            for l in c["legs"]:
                want *= l["price"] + cfb.COMBO_MARGIN
            self.assertAlmostEqual(c["price"], want)

    def test_too_few_games_gives_no_combo(self):
        self.assertEqual(cfb.build_combos([cand(1, .8), cand(2, .75)]), [])


class FakeClient:
    def __init__(self, results):
        self.results = results

    def market(self, t):
        return {"result": self.results.get(t, "")}


class TestRecordSettle(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        self.cands = [cand(i, p) for i, p in enumerate([.85, .8, .75])]
        self.combos = cfb.build_combos(self.cands)

    def test_record_once_and_not_after_kickoff(self):
        before = 1_793_800_000 - 10 * 86400
        p, c = cfb.record(self.store, self.cands, self.combos, before)
        self.assertEqual((p, c), (3, 1))
        self.assertEqual(cfb.record(self.store, self.cands, self.combos, before), (0, 0))
        late = cfb.record(Store(os.path.join(tempfile.mkdtemp(), "u.db")), self.cands, self.combos, 1_900_000_000)
        self.assertEqual(late, (0, 0))

    def test_combo_loses_on_one_leg_and_wins_on_all(self):
        before = 1_793_800_000 - 10 * 86400
        cfb.record(self.store, self.cands, self.combos, before)
        after = 1_900_000_000
        cfb.settle(self.store, FakeClient({"T0": "yes", "T1": "yes", "T2": "yes"}), after)
        row = self.store.q("SELECT * FROM cfb_combos")[0]
        self.assertEqual(row["status"], "settled")
        self.assertGreater(row["pnl"], 0)
        s = cfb.summary(self.store)
        self.assertEqual(s["picks"]["wins"], 3)

    def test_one_loser_sinks_the_combo(self):
        before = 1_793_800_000 - 10 * 86400
        cfb.record(self.store, self.cands, self.combos, before)
        cfb.settle(self.store, FakeClient({"T0": "yes", "T1": "no", "T2": "yes"}), 1_900_000_000)
        row = self.store.q("SELECT * FROM cfb_combos")[0]
        self.assertAlmostEqual(row["pnl"], -(row["stake"] + row["fee"]), places=2)

    def test_run_never_raises(self):
        class Bad:
            def market(self, t):
                raise RuntimeError("x")

            def _paged(self, *a, **k):
                raise RuntimeError("x")
        self.assertIsNone(cfb.run(self.store, Bad(), now=NOW, fetch=lambda *a, **k: Resp([])))


if __name__ == "__main__":
    unittest.main()
