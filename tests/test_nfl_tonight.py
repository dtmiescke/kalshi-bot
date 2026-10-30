import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from bot import nfl_tonight as nfl
from bot.storage import Store

NOW = 1_793_500_000
TIP = "2026-11-01T18:00:00Z"                       # after NOW


def log(date, **stats):
    return {"date": date, "stats": stats}


def payload():
    return {"names": ["receptions", "receivingYards", "rushingAttempts"],
            "events": {"1": {"gameDate": "2026-10-04T17:00:00.000+00:00"}, "2": {"gameDate": "2026-10-11T17:00:00.000+00:00"}},
            "seasonTypes": [{"categories": [{"events": [{"eventId": "2", "stats": ["6", "88", "0"]},
                                                        {"eventId": "1", "stats": ["4", "1,020", "-"]},
                                                        {"eventId": "9", "stats": ["1", "1", "1"]}]}]}]}


class TestMaths(unittest.TestCase):
    def test_gamelog_is_parsed_by_stat_name_oldest_first_and_bad_cells_dropped(self):
        games = nfl.parse_gamelog(payload())
        self.assertEqual([g["date"] for g in games], ["2026-10-04", "2026-10-11"])      # event 9 has no date: skipped
        self.assertEqual(games[0]["stats"], {"receptions": 4.0, "receivingYards": 1020.0})   # "-" dropped, "1,020" read
        self.assertEqual(nfl.values(games, "receptions"), [4.0, 6.0])

    def test_recent_games_count_more_and_last_season_is_a_small_prior(self):
        cur = [log(f"2026-09-{d:02d}", receivingYards=y) for d, y in ((6, 40), (13, 40), (20, 40), (27, 100))]
        flat = [log(f"2026-09-{d:02d}", receivingYards=40) for d in (6, 13, 20, 27)]
        mu, info = nfl.projection(cur, "receivingYards")
        self.assertGreater(mu, nfl.projection(flat, "receivingYards")[0])
        self.assertLess(mu, 100)
        self.assertEqual(info["games_this_season"], 4)
        old = [log(f"2025-10-{d:02d}", receivingYards=80) for d in range(1, 10)]
        mu2, _ = nfl.projection(old + flat, "receivingYards")
        self.assertGreater(mu2, 40)                                                       # last season pulls it up a little
        self.assertLess(mu2, 60)
        self.assertEqual(nfl.projection(flat[:2], "receivingYards"), (None, {}))          # too little history

    def test_probability_follows_the_projection_and_needs_history(self):
        games = [log(f"2026-09-{d:02d}", receivingYards=y) for d, y in enumerate([30, 50, 70, 90, 40, 60, 80, 100, 55, 65], 1)]
        avg = sum(nfl.values(games, "receivingYards")) / 10
        low = nfl.prob_at_least(games, "receivingYards", avg, 100)
        mid = nfl.prob_at_least(games, "receivingYards", avg, 60)
        up = nfl.prob_at_least(games, "receivingYards", avg * 1.5, 60)
        self.assertLess(low, mid)
        self.assertGreater(up, mid)
        self.assertIsNone(nfl.prob_at_least(games[:5], "receivingYards", avg, 60))

    def test_game_script_is_bounded_and_split_by_pass_and_rush(self):
        self.assertEqual(nfl.team_totals(48, -3), (25.5, 22.5))                          # home favoured by 3 of 48
        self.assertEqual(nfl.team_totals(None, -3), None)
        self.assertGreater(nfl.script_factor("pass", 30, 0), 1)
        self.assertLess(nfl.script_factor("pass", 16, 0), 1)
        self.assertEqual(nfl.script_factor("pass", 99, 0), 1 + nfl.SCRIPT_MAX)
        self.assertGreater(nfl.script_factor("rush", 22, 7), 1)                          # favourites run more
        self.assertEqual(nfl.script_factor("rush", None, None), 1.0)


def mk(ticker, who, line, bid, ask):
    return {"ticker": ticker, "yes_sub_title": f"{who}: {int(line + .5)}+", "floor_strike": line,
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "strike_type": "greater"}


class TestReport(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.game = {"id": "g1", "tip": TIP, "state": "pre", "ou": 48, "spread": -3,
                     "home": {"id": "1", "abbr": "BUF", "name": "Buffalo Bills"},
                     "away": {"id": "2", "abbr": "BAL", "name": "Baltimore Ravens"}}

    def world(self, markets, logs=None, injuries=None, series="KXNFLRECYDS"):
        ros = {"1": [{"id": "a1", "name": "Star Receiver", "pos": "WR"}, {"id": "a2", "name": "Second Receiver", "pos": "WR"}],
               "2": [{"id": "b1", "name": "Other Receiver", "pos": "WR"}]}
        ev = {"event_ticker": "KXNFLRECYDS-26NOV01BALBUF", "title": "Baltimore vs Buffalo: Receiving Yards", "markets": markets}
        steady = [log(f"2026-09-{d:02d}", receivingYards=80.0 + (d % 3) * 10) for d in range(1, 13)]
        client = MagicMock()
        patches = [patch.object(nfl, "slate", return_value=[self.game]),
                   patch.object(nfl, "roster", side_effect=lambda tid, fetch=None: ros[tid]),
                   patch.object(nfl, "player_games", side_effect=lambda pid, fetch=None: (logs or {}).get(pid, steady)),
                   patch.object(nfl.nba, "injuries_feed", return_value=injuries or {}),
                   patch.object(nfl.nba, "kalshi_events", side_effect=lambda c, s: [ev] if s == series else [])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return client

    def test_a_cheap_line_the_projection_clears_becomes_a_pick(self):
        client = self.world([mk("T-90", "Star Receiver", 89.5, .40, .45)])         # he tops 90 about two games in three: priced 45
        rep = nfl.build_report(client, self.store, NOW)
        self.assertEqual(len(rep["picks"]), 1)
        p = rep["picks"][0]
        self.assertEqual((p["side"], p["label"]), ("yes", "Star Receiver 90+ receiving yards (YES)"))
        self.assertGreater(p["edge"], nfl.MIN_EDGE)
        self.assertGreater(p["cautious_edge"], 0)
        self.assertEqual(rep["games"][0]["total"], 48)

    def test_extreme_edges_illiquid_books_and_a_player_who_is_out_are_skipped(self):
        client = self.world([mk("T-60", "Star Receiver", 59.5, .50, .55)])         # a sure thing priced at 55: an edge past the cap, i.e. suspect
        rep = nfl.build_report(client, self.store, NOW)
        self.assertEqual(rep["picks"], [])
        self.assertEqual(rep["skipped_extreme"], 1)
        client = self.world([mk("T-90", "Star Receiver", 89.5, .20, .60)])         # a 40-cent-wide book
        self.assertEqual(nfl.build_report(client, self.store, NOW)["picks"], [])
        client = self.world([mk("T-90", "Star Receiver", 89.5, .40, .45)],
                            injuries={"Buffalo Bills": [{"name": "Star Receiver", "status": "Out", "comment": "", "level": "out"}]})
        self.assertEqual(nfl.build_report(client, self.store, NOW)["picks"], [])

    def test_picks_are_capped_per_game(self):
        mkts = [mk("A-90", "Star Receiver", 89.5, .40, .45), mk("B-90", "Second Receiver", 89.5, .40, .45)]
        self.assertEqual(len(nfl.build_report(self.world(mkts), self.store, NOW)["picks"]), 2)
        with patch.object(nfl, "MAX_PER_GAME", 1):
            rep = nfl.build_report(self.world(mkts), self.store, NOW)
        self.assertEqual(len(rep["picks"]), 1)

    def test_a_started_game_is_never_rated(self):
        self.game["state"] = "in"
        rep = nfl.build_report(self.world([mk("T-90", "Star Receiver", 89.5, .40, .45)]), self.store, NOW)
        self.assertEqual((rep["games"], rep["picks"]), ([], []))


class TestAccount(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.pick = {"ticker": "T1", "game": "A at B", "kind": "prop", "label": "X 60+ (YES)", "side": "yes", "price": 0.5,
                     "contracts": 10, "fee": 0.2, "p_model": 0.7, "edge": 0.15, "tip": TIP}

    def test_a_pick_is_recorded_once_and_only_before_kickoff(self):
        rep = {"picks": [self.pick, {**self.pick, "ticker": "T2", "tip": "2026-10-01T18:00:00Z"}]}
        self.assertEqual(nfl.record_picks(self.store, rep, NOW), 1)                  # the started game is refused
        self.assertEqual(nfl.record_picks(self.store, rep, NOW + 60), 0)             # and a re-run adds nothing
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM nfl_picks")[0]["n"], 1)

    def test_settles_from_kalshi_and_summarises(self):
        nfl.record_picks(self.store, {"picks": [self.pick, {**self.pick, "ticker": "T3", "side": "no"}]}, NOW)
        client = MagicMock()
        client.market.side_effect = lambda t: {"result": "yes"}
        late = NOW + 40 * 3600
        self.assertEqual(nfl.settle(self.store, client, NOW + 3600), 0)             # before the game ends: nothing
        self.assertEqual(nfl.settle(self.store, client, late), 2)
        s = nfl.summary(self.store)["account"]
        self.assertEqual((s["picks"], s["settled"], s["wins"]), (2, 2, 1))
        rows = {r["ticker"]: r["pnl"] for r in self.store.q("SELECT ticker, pnl FROM nfl_picks")}
        self.assertEqual(rows["T1"], round(10 * .5 - .2, 2))
        self.assertEqual(rows["T3"], -5.2)

    def test_run_is_throttled_and_never_raises(self):
        client = MagicMock()
        with patch.object(nfl, "build_report", return_value={"picks": [self.pick], "games": []}):
            self.assertEqual(nfl.run(self.store, client, NOW, every=7200)["recorded"], 1)
            self.assertIsNone(nfl.run(self.store, client, NOW + 60, every=7200))
        with patch.object(nfl, "build_report", side_effect=RuntimeError("boom")):
            self.assertIsNone(nfl.run(self.store, client, NOW + 99999, every=7200))


if __name__ == "__main__":
    unittest.main()


class TestMix(unittest.TestCase):
    def test_a_game_is_not_five_of_the_same_bet(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        base = {"game": "A at B", "kind": "prop", "side": "yes", "price": 0.5, "contracts": 10, "fee": 0.2, "p_model": 0.7,
                "edge": 0.15, "tip": TIP}
        same_stat = [{**base, "ticker": f"S{i}", "label": f"Runner {i} 60+ rushing yards (YES)"} for i in range(5)]
        self.assertEqual(nfl.record_picks(store, {"picks": same_stat}, NOW), nfl.MAX_PER_STAT)
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        stats = ["passing yards", "receiving yards", "rushing yards", "receptions", "rushing attempts"]
        same_side = [{**base, "side": "no", "ticker": f"D{i}", "label": f"Player {i} 60+ {stats[i]} (NO)"} for i in range(5)]
        self.assertEqual(nfl.record_picks(store, {"picks": same_side}, NOW), nfl.MAX_SAME_SIDE)
