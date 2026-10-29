"""NBA tonight: the projection maths, the report on a fake slate, and the shadow account (record once, settle, never
pick a game that has started)."""
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import nba_tonight as nba
from bot.storage import Store


def game(date, mins, pts=0, reb=0, ast=0, tpm=0):
    return {"date": date, "season_type": "x", "MIN": mins, "PTS": pts, "REB": reb, "AST": ast, "STL": 0, "BLK": 0, "3PM": tpm, "FTM": 0}


def season(n, start, pts, mins=30, step=2):
    return [game(f"{start[:8]}{(i % 27) + 1:02d}", mins, pts) for i in range(n)]


class TestMaths(unittest.TestCase):
    def test_gamelog_parsing_skips_preseason_and_reads_made_threes(self):
        payload = {"labels": ["MIN", "FG", "FG%", "3PT", "3P%", "FT", "FT%", "REB", "AST", "BLK", "STL", "PF", "TO", "PTS"],
                   "events": {"1": {"gameDate": "2026-10-28T00:00:00Z"}, "2": {"gameDate": "2026-10-01T00:00:00Z"}},
                   "seasonTypes": [{"displayName": "2026-27 Regular Season", "categories": [{"events": [
                       {"eventId": "1", "stats": ["34", "6-11", "54.5", "4-9", "44.4", "0-1", "0.0", "7", "5", "1", "2", "2", "1", "26"]}]}]},
                       {"displayName": "2026-27 Preseason", "categories": [{"events": [
                           {"eventId": "2", "stats": ["10", "1-2", "50", "0-1", "0", "0-0", "0", "1", "1", "0", "0", "0", "0", "2"]}]}]}]}
        g = nba.parse_gamelog(payload)
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]["PTS"], g[0]["REB"], g[0]["AST"], g[0]["3PM"], g[0]["MIN"]), (26, 7, 5, 4, 34))

    def test_a_bigger_role_shows_up_through_minutes(self):
        old = [game(f"2026-02-{i+1:02d}", 24, pts=10) for i in range(20)]               # 10 points in 24 minutes last season
        new = [game(f"2026-10-{i+20:02d}", 36, pts=15) for i in range(4)]               # 15 in 36 minutes now
        mu, info = nba.projection(old + new, ("PTS",), "2026-10-01")
        self.assertGreater(mu, 13.0)                                                     # well above last season's 10
        self.assertEqual(info["minutes"], 31.7)                                          # 0.7 x last 5 (33.6) + 0.3 x last 15 (27.2)
        self.assertEqual(info["games_this_season"], 4)

    def test_too_little_history_is_not_projected(self):
        self.assertEqual(nba.projection([game("2026-10-20", 30, 10)], ("PTS",))[0], None)

    def test_probability_follows_the_projection_and_the_line(self):
        games = [game(f"2026-02-{i % 27 + 1:02d}", 30, pts=p) for i, p in enumerate([10, 20, 15, 25, 18, 22, 12, 17, 19, 21] * 3)]
        mu, info = nba.projection(games, ("PTS",), "2026-10-01")
        low = nba.prob_at_least(games, ("PTS",), mu, info["minutes"], 10)
        mid = nba.prob_at_least(games, ("PTS",), mu, info["minutes"], 18)
        high = nba.prob_at_least(games, ("PTS",), mu, info["minutes"], 28)
        self.assertGreater(low, mid)
        self.assertGreater(mid, high)
        self.assertGreater(low, 0.9)
        self.assertLess(high, 0.1)

    def test_edges_use_the_real_ask_and_fee(self):
        e = nba.side_edges(0.70, 0.55, 0.57)
        self.assertEqual(e[0]["side"], "yes")
        self.assertGreater(e[0]["edge"], 0.10)
        self.assertLess(e[0]["edge"], 0.70 - 0.57)                                       # the fee comes off
        self.assertEqual(nba.side_edges(0.30, 0.55, 0.57)[0]["side"], "no")

    def test_team_model_blends_seasons_and_favours_the_home_side(self):
        s = nba.team_strength({"net": -6.0, "games": 4}, {"net": 4.0})
        self.assertGreater(s, 2.0)                                                       # four games barely move last season
        self.assertAlmostEqual(nba.home_win_prob(0, 0), 0.5825, places=3)                # home court alone is worth about 58%
        self.assertGreater(nba.home_win_prob(5, 0), nba.home_win_prob(0, 5))


# --- a fake slate -------------------------------------------------------------------------------------------------

HOME = {"id": "1", "abbr": "ATL", "name": "Atlanta Hawks"}
AWAY = {"id": "5", "abbr": "CLE", "name": "Cleveland Cavaliers"}
TIP = "2026-10-30T00:00Z"


def mkt(ticker, label, bid, ask):
    return {"ticker": ticker, "yes_sub_title": label, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask)}


class FakeClient:
    def __init__(self, results=None):
        self.results = results or {}

    def _paged(self, path, key, params, max_pages=1):
        s = params["series_ticker"]
        if s == "KXNBAGAME":
            return [{"title": "Cleveland vs Atlanta", "event_ticker": "KXNBAGAME-26OCT29CLEATL",
                     "markets": [mkt("ML-CLE", "Cleveland", 0.40, 0.42), mkt("ML-ATL", "Atlanta", 0.58, 0.60)]}]
        if s == "KXNBAPTS":
            return [{"title": "Cleveland vs Atlanta: Points", "event_ticker": "KXNBAPTS-26OCT29CLEATL",
                     "markets": [mkt("P-10", "Star Player: 10+", 0.93, 0.95), mkt("P-20", "Star Player: 20+", 0.28, 0.30),
                                 mkt("P-30", "Star Player: 30+", 0.03, 0.05)]}]
        return []

    def market(self, ticker):
        return {"result": self.results.get(ticker)}


INJURIES = {"Cleveland Cavaliers": [{"name": "Teammate Out", "status": "Out", "comment": "out", "level": "out"}]}


def fake_world():
    games_a = [game(f"2026-02-{i % 27 + 1:02d}", 34, pts=p) for i, p in enumerate([26, 30, 24, 28, 32, 27, 25, 29, 31, 26] * 3)]
    return [
        patch.object(nba, "tonight_games", lambda day=None, fetch=None: [{"id": "g1", "tip": TIP, "status": "STATUS_SCHEDULED", "home": HOME, "away": AWAY}]),
        patch.object(nba, "standings", lambda season, fetch=None: {"1": {"net": 1.0, "games": 5, "ppg": 118.0, "oppg": 117.0, "record": "3-2", "name": "Atlanta Hawks"},
                                                                  "5": {"net": 3.0, "games": 5, "ppg": 119.0, "oppg": 116.0, "record": "4-1", "name": "Cleveland Cavaliers"}}),
        patch.object(nba, "roster", lambda tid, fetch=None: [{"id": "77", "name": "Star Player", "injuries": []},
                                                             {"id": "78", "name": "Teammate Out", "injuries": []}] if tid == "5" else []),
        patch.object(nba, "player_games", lambda aid, fetch=None: games_a),
        patch.object(nba, "injuries_feed", lambda fetch=None: INJURIES),
    ]


class TestReport(unittest.TestCase):
    def build(self):
        ps = fake_world()
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])
        return nba.build_report(FakeClient(), None, now=1_793_300_000, day="20261029")

    def test_the_slate_has_the_game_the_moneyline_and_the_players_ladder(self):
        rep = self.build()
        self.assertEqual(len(rep["games"]), 1)
        g = rep["games"][0]
        self.assertIsNotNone(g["ml"])
        self.assertEqual(g["ml"]["options"][0]["ticker"] in ("ML-CLE", "ML-ATL"), True)
        row = g["props"][0]
        self.assertEqual(row["player"], "Star Player")
        self.assertGreater(row["projection"], 24)
        self.assertEqual([r["line"] for r in row["rungs"]], [10, 20, 30])
        self.assertIn("fit_line", row)

    def test_a_clear_gap_between_model_and_price_becomes_a_pick(self):
        rep = self.build()
        labels = [p["label"] for p in rep["picks"]]
        self.assertTrue(any("20+" in l and "YES" in l for l in labels), labels)           # a 27-point scorer priced at 30% for 20+
        for p in rep["picks"]:
            if p["kind"] == "prop":
                self.assertGreaterEqual(p["edge"], nba.MIN_EDGE)
                self.assertGreater(p["cautious_edge"], 0)


class TestLineups(unittest.TestCase):
    def test_injury_levels_are_read_from_status_and_comment(self):
        feed = {"injuries": [{"displayName": "Team A", "injuries": [
            {"status": "Out", "shortComment": "knee surgery", "athlete": {"displayName": "A One"}},
            {"status": "Day-To-Day", "shortComment": "Two is questionable for Thursday", "athlete": {"displayName": "A Two"}},
            {"status": "Day-To-Day", "shortComment": "Three is doubtful for Thursday", "athlete": {"displayName": "A Three"}},
            {"status": "Probable", "shortComment": "sore back, expected to play", "athlete": {"displayName": "A Four"}}]}]}

        class R:
            status_code = 200

            def json(self):
                return feed
        out = nba.injuries_feed(lambda url, params=None, timeout=None: R())["Team A"]
        self.assertEqual([r["level"] for r in out], ["out", "doubt", "out", "ok"])

    def test_missing_minutes_raise_a_teammates_projection_but_never_past_the_cap(self):
        games = [game(f"2026-02-{i % 27 + 1:02d}", 24, pts=12) for i in range(25)]
        base, info = nba.projection(games, ("PTS",))
        more, info2 = nba.projection(games, ("PTS",), extra_min=3.0)
        huge, info3 = nba.projection(games, ("PTS",), extra_min=30.0)
        self.assertGreater(more, base)
        self.assertEqual(info2["minutes"], 27.0)
        self.assertEqual(info3["minutes"], nba.MINUTES_CAP)                              # never bumped past 29
        starters = [game(f"2026-02-{i % 27 + 1:02d}", 35, pts=20) for i in range(25)]
        self.assertEqual(nba.projection(starters, ("PTS",), extra_min=8.0)[1]["minutes"], 35.0)   # his own minutes are never cut
        games = [game(f"2026-02-{i % 27 + 1:02d}", 24, pts=12) for i in range(25)]
        rebounds = [{**g, "REB": 6.0} for g in games]
        self.assertGreater(nba.projection(rebounds, ("REB",), extra_min=3.0)[0], nba.projection(rebounds, ("REB",))[0])
        self.assertLess(nba.projection(rebounds, ("REB",), extra_min=3.0)[0] / nba.projection(rebounds, ("REB",))[0],
                        nba.projection(games, ("PTS",), extra_min=3.0)[0] / base)       # scoring also gets a usage bump; rebounds only minutes

    def test_a_star_who_is_out_costs_his_team_strength(self):
        self.assertAlmostEqual(nba.net_loss(28, 7), 0.12 * (28 + 10.5), places=3)
        self.assertGreater(nba.net_loss(28, 7), nba.net_loss(8, 1))

    def test_the_report_shows_who_is_out_and_passes_on_their_minutes(self):
        ps = fake_world()
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])
        rep = nba.build_report(FakeClient(), None, now=1_793_300_000, day="20261029")
        g = rep["games"][0]
        self.assertEqual([a["name"] for a in g["lineups"]["away"]["absent"]], ["Teammate Out"])
        self.assertLess(g["lineups"]["away"]["net_adjustment"], 0)                       # Cleveland is weaker without him
        star = g["props"][0]
        self.assertGreater(star["extra_minutes"], 0)                                      # the Star picks up some of his minutes
        self.assertLessEqual(star["minutes"], max(nba.MINUTES_CAP, 34.0))                # never bumped past the cap (his own minutes aside)

    def test_a_player_who_is_out_or_uncertain_is_never_picked(self):
        for level in ("out", "doubt"):
            feed = {"Cleveland Cavaliers": [{"name": "Star Player", "status": "x", "comment": "x", "level": level}]}
            ps = fake_world()[:3] + [patch.object(nba, "player_games", lambda aid, fetch=None: [game(f"2026-02-{i % 27 + 1:02d}", 34, pts=p) for i, p in enumerate([26, 30, 24, 28, 32, 27, 25, 29, 31, 26] * 3)]),
                                     patch.object(nba, "injuries_feed", lambda fetch=None, feed=feed: feed)]
            for p in ps:
                p.start()
            try:
                rep = nba.build_report(FakeClient(), None, now=1_793_300_000, day="20261029")
            finally:
                for p in ps:
                    p.stop()
            self.assertFalse([p for p in rep["picks"] if "Star Player" in p["label"]], level)


class TestShadowAccount(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.pick = {"kind": "prop", "game": "A at B", "ticker": "P-20", "label": "Star 20+ points (YES)", "side": "yes",
                     "price": 0.30, "contracts": 16, "fee": 0.28, "p_model": 0.80, "edge": 0.48, "tip": "2026-10-30T00:00Z"}

    def test_picks_are_recorded_once_and_never_after_tip_off(self):
        tip = 1_793_318_400                                                                # 2026-10-30T00:00Z
        self.assertEqual(nba.record_picks(self.store, {"picks": [self.pick]}, now=tip - 3600), 1)
        self.assertEqual(nba.record_picks(self.store, {"picks": [self.pick]}, now=tip - 1800), 0)    # same ticker: not again
        late = {**self.pick, "ticker": "P-25"}
        self.assertEqual(nba.record_picks(self.store, {"picks": [late]}, now=tip + 60), 0)           # game already started
        self.assertEqual(self.store.q("SELECT COUNT(*) AS n FROM nba_picks")[0]["n"], 1)

    def test_settling_pays_wins_and_charges_losses(self):
        tip = 1_793_318_400
        nba.record_picks(self.store, {"picks": [self.pick, {**self.pick, "ticker": "P-25", "label": "x"}]}, now=tip - 3600)
        later = tip + 4 * 3600
        self.assertEqual(nba.settle(self.store, FakeClient({}), now=later), 0)                       # no result yet: stays open
        self.assertEqual(nba.settle(self.store, FakeClient({"P-20": "yes", "P-25": "no"}), now=later), 2)
        won = self.store.q("SELECT pnl, status FROM nba_picks WHERE ticker='P-20'")[0]
        lost = self.store.q("SELECT pnl FROM nba_picks WHERE ticker='P-25'")[0]
        self.assertAlmostEqual(won["pnl"], 16 * 0.70 - 0.28, places=2)
        self.assertAlmostEqual(lost["pnl"], -(16 * 0.30 + 0.28), places=2)
        s = nba.summary(self.store)
        self.assertEqual((s["account"]["settled"], s["account"]["wins"]), (2, 1))

    def test_a_pass_never_raises_and_respects_its_interval(self):
        class Boom:
            def _paged(self, *a, **k):
                raise RuntimeError("down")

            def market(self, t):
                raise RuntimeError("down")
        self.assertIsNone(nba.run(self.store, Boom(), now=1_793_300_000))                            # swallowed
        self.assertIsNone(nba.run(self.store, Boom(), now=1_793_300_100))                            # inside the interval: skipped


if __name__ == "__main__":
    unittest.main()
