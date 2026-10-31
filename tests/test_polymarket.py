import unittest

from bot import polymarket as pm


def ev(names, prices, liq="5000", kind="moneyline"):
    return {"endDate": "2026-11-01T00:00:00Z", "markets": [{"sportsMarketType": kind, "outcomes": str(names).replace("'", '"'),
                                                          "outcomePrices": str(prices).replace("'", '"'), "liquidity": liq}]}


class T(unittest.TestCase):
    def test_reads_only_liquid_live_moneylines(self):
        games = pm.games_from_events([ev(["Arizona", "Texas Tech"], ["0.375", "0.625"]),
                                      ev(["A", "B"], ["0", "1"]),                       # settled
                                      ev(["C", "D"], ["0.5", "0.5"], liq="10"),         # thin
                                      ev(["E", "F"], ["0.5", "0.5"], kind="spreads")])
        self.assertEqual(len(games), 1)
        self.assertAlmostEqual(sum(games[0]["teams"].values()), 1.0)

    def test_chance_matches_by_name_and_opponent(self):
        games = pm.games_from_events([ev(["Arizona", "Texas Tech"], ["0.375", "0.625"])])
        self.assertAlmostEqual(pm.chance(games, "Texas Tech Red Raiders", "Arizona Wildcats"), 0.625)
        self.assertIsNone(pm.chance(games, "Ohio State", "USC"))

    def test_cached_never_raises_and_falls_back(self):
        class Store:
            kv = {}

            def get_kv(self, k, d=None):
                return self.kv.get(k, d)

            def set_kv(self, k, v):
                self.kv[k] = v

        def bad(*a, **k):
            raise RuntimeError("down")
        self.assertEqual(pm.cached(Store(), now=1, get=bad), [])


if __name__ == "__main__":
    unittest.main()
