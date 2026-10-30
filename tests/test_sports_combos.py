import os
import tempfile
import unittest

from bot import sports_combos as sc
from bot.storage import Store

NOW = 1_793_500_000
TIP = NOW + 6 * 3600


def leg(i, game=None, side="no", price=0.5, p=0.7, edge=0.15, stat="points", player=None):
    return {"ticker": f"T{i}", "game": game or f"G{i}", "player": player or f"Player {i}", "side": side,
            "label": f"{player or f'Player {i}'} 10+ {stat} ({side.upper()})", "price": price, "p_model": p, "edge": edge, "tip": TIP}


STATS = ["points", "rebounds", "assists", "threes", "steals", "blocks", "pts+reb+ast"]


class TestPricing(unittest.TestCase):
    def test_cost_is_the_product_and_every_leg_is_pulled_toward_the_price(self):
        legs = [leg(1, price=.5, p=.7), leg(2, price=.4, p=.6)]
        pr = sc.price_combo(legs)
        self.assertAlmostEqual(pr["price"], .2)
        self.assertAlmostEqual(pr["p_model"], .42)
        self.assertAlmostEqual(pr["p_cautious"], .6 * .5)                       # .5+.5(.7-.5)=.6 and .4+.5(.6-.4)=.5
        self.assertLess(pr["edge"], pr["p_cautious"] - pr["price"])              # the fee is taken off
        self.assertEqual(pr["contracts"], int(sc.STAKE // .2))
        self.assertLessEqual(sc.price_combo([leg(3, price=.05, p=.1), leg(4, price=.05, p=.1)])["contracts"], sc.MAX_CONTRACTS)


class TestBuild(unittest.TestCase):
    def pool(self, n=10):
        return [leg(i, side="yes" if i % 2 else "no", stat=STATS[i % 7]) for i in range(n)]

    def test_combos_have_different_games_players_and_stats_and_a_mix_of_sides(self):
        for c in sc.build(self.pool()):
            legs = c["legs"]
            self.assertEqual(len({p["game"] for p in legs}), len(legs))
            self.assertEqual(len({p["player"] for p in legs}), len(legs))
            self.assertEqual(len({sc.stat_of(p["label"]) for p in legs}), len(legs))
            if len(legs) >= 3:
                sides = [p["side"] for p in legs]
                self.assertLessEqual(max(sides.count("yes"), sides.count("no")), (len(legs) * 2) // 3)
            self.assertGreaterEqual(c["edge"], sc.MIN_EDGE)

    def test_legs_in_one_game_never_share_a_combo(self):
        picks = [leg(1, game="G", stat="points"), leg(2, game="G", stat="assists"), leg(3, game="G", stat="rebounds")]
        self.assertEqual(sc.build(picks), [])

    def test_the_day_is_not_one_bet_reshuffled(self):
        combos = sc.build(self.pool(12))
        self.assertTrue(combos)
        uses = {}
        for c in combos:
            for p in c["legs"]:
                uses[p["ticker"]] = uses.get(p["ticker"], 0) + 1
        self.assertLessEqual(max(uses.values()), sc.MAX_LEG_USES)
        for i, a in enumerate(combos):
            for b in combos[i + 1:]:
                self.assertLessEqual(len({p["ticker"] for p in a["legs"]} & {p["ticker"] for p in b["legs"]}), 1)

    def test_game_winners_extreme_edges_and_thin_edges_are_not_legs(self):
        ml = leg(1)
        ml["player"] = None
        picks = [ml, leg(2, edge=0.45), leg(3, edge=0.03), leg(4, price=0.99, p=1.0), leg(5), leg(6, stat="assists")]
        for c in sc.build(picks):
            self.assertTrue({p["ticker"] for p in c["legs"]} <= {"T5", "T6"})


class TestAccount(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.store.x("""CREATE TABLE IF NOT EXISTS nba_picks (ticker TEXT PRIMARY KEY, ts INTEGER, game TEXT, kind TEXT, label TEXT,
                        side TEXT, price REAL, contracts INTEGER, stake REAL, fee REAL, p_model REAL, edge REAL, tip_ts INTEGER,
                        status TEXT, result TEXT, pnl REAL, settled_ts INTEGER)""")
        for i in range(6):
            self.store.x("INSERT INTO nba_picks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',NULL,NULL,NULL)",
                         (f"T{i}", NOW, f"G{i}", "prop", f"Player {i} 10+ {STATS[i]} ({'YES' if i % 2 else 'NO'})",
                          "yes" if i % 2 else "no", .5, 10, 5.0, .1, .7, .15, TIP))

    def test_combos_are_recorded_once_and_only_before_the_games(self):
        out = sc.run(self.store, NOW, every=0)
        self.assertGreater(out["recorded"], 0)
        n = self.store.q("SELECT COUNT(*) n FROM sports_combos")[0]["n"]
        self.assertEqual(sc.run(self.store, NOW + 10, every=0)["recorded"], 0)
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM sports_combos")[0]["n"], n)
        self.assertEqual(sc.run(self.store, TIP + 1, every=0)["recorded"], 0)   # the games have started: nothing new
        self.assertIsNone(sc.run(self.store, TIP + 2, every=3600))               # throttled

    def test_a_combo_loses_with_its_first_lost_leg_and_wins_only_when_all_win(self):
        sc.run(self.store, NOW, every=0)
        combos = self.store.q("SELECT * FROM sports_combos ORDER BY n_legs")
        first = combos[0]
        import json
        tickers = [g["ticker"] for g in json.loads(first["legs"])]
        self.store.x("UPDATE nba_picks SET status='settled', pnl=3.0 WHERE ticker=?", (tickers[0],))
        self.assertEqual(sc.settle(self.store, TIP + 3600), 0)                    # one leg won, the other is not settled yet
        self.store.x("UPDATE nba_picks SET status='settled', pnl=-5.0 WHERE ticker=?", (tickers[1],))
        sc.settle(self.store, TIP + 3600)
        row = self.store.q("SELECT * FROM sports_combos WHERE key=?", (first["key"],))[0]
        self.assertEqual((row["status"], row["pnl"]), ("settled", round(-(row["stake"] + row["fee"]), 2)))
        for t in tickers:
            self.store.x("UPDATE nba_picks SET status='settled', pnl=3.0 WHERE ticker=?", (t,))
        second = [c for c in combos if c["key"] != first["key"]]
        self.assertTrue(second)

    def test_summary_lists_open_combos_with_their_legs(self):
        sc.run(self.store, NOW, every=0)
        s = sc.summary(self.store)
        self.assertEqual(s["account"]["open"], len(s["open"]))
        self.assertTrue(all(len(c["legs"]) >= 2 for c in s["open"]))

    def test_run_never_raises(self):
        broken = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.assertIsNotNone(sc.run(broken, NOW, every=0))                       # no pick tables at all: nothing to build


if __name__ == "__main__":
    unittest.main()
