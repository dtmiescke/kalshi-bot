"""In-game (live) paper betting: live win model, matching ESPN to Kalshi, the
safety gates, grading, and the per-game learning loop."""
import os
import tempfile
import unittest
from unittest import mock

from bot import live_games as lg
from bot.config import Settings
from bot.engine import Engine
from bot.storage import Store

NOW = 1_800_000_000
START = NOW - 2 * 3600
EVENT = "KXNBAGAME-26OCT25NYKBOS"


def espn_payload(state="in", period=4, clock="2:00", bos=100, nyk=85):
    return {"events": [{
        "id": "401", "name": "New York Knicks at Boston Celtics", "shortName": "NYK @ BOS",
        "date": "2027-01-15T00:00Z",
        "status": {"displayClock": clock, "period": period,
                   "type": {"state": state, "description": "In Progress", "detail": "Q4"}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "score": str(bos),
             "team": {"displayName": "Boston Celtics", "abbreviation": "BOS"}},
            {"homeAway": "away", "score": str(nyk),
             "team": {"displayName": "New York Knicks", "abbreviation": "NYK"}}]}]}]}


class FakeESPN:
    def __init__(self, scoreboard=None, home_wp=0.97):
        self.scoreboard = scoreboard if scoreboard is not None else espn_payload()
        self.home_wp = home_wp
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if "/nba/scoreboard" in url:
            return self.scoreboard
        if "scoreboard" in url:
            return {"events": []}
        if "summary" in url:
            return {"winprobability": [] if self.home_wp is None else
                    [{"homeWinPercentage": 0.5}, {"homeWinPercentage": self.home_wp}]}
        raise AssertionError(url)


class FakeKalshi:
    def __init__(self, bos_ask=0.80):
        self.bos_ask = bos_ask
        self.results = {}

    def sports_events(self, series):
        if series != "KXNBAGAME":
            return []
        a = self.bos_ask
        return [{"event_ticker": EVENT, "series_ticker": series, "markets": [
            {"ticker": EVENT + "-BOS", "yes_sub_title": "Boston", "status": "active",
             "yes_bid": a - 0.02, "yes_ask": a, "close_time": "2027-01-15T06:00Z"},
            {"ticker": EVENT + "-NYK", "yes_sub_title": "New York", "status": "active",
             "yes_bid": 1 - a, "yes_ask": 1 - a + 0.02, "close_time": "2027-01-15T06:00Z"}]}]

    def orderbook(self, ticker):
        a = self.bos_ask if ticker.endswith("BOS") else 1 - self.bos_ask + 0.02
        # YES ask = 1 - best NO bid
        return {"yes": [(round(a - 0.02, 2), 500)], "no": [(round(1 - a, 2), 500)]}

    def market(self, ticker):
        return {"result": self.results.get(ticker, "")}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()
        self.s.mode = "paper"
        for team, p in (("BOS", 0.60), ("NYK", 0.40)):     # pre-game closing prices
            t = EVENT + "-" + team
            self.st.add_market_snapshot({"ts": START - 600, "ticker": t, "event_ticker": EVENT,
                                         "series": "KXNBAGAME", "title": "", "team": team,
                                         "yes_bid": p - 0.01, "yes_ask": p + 0.01,
                                         "last_price": p, "volume": 0, "open_interest": 0,
                                         "close_time": None, "status": "active"})
            self.st.set_kv(f"start_{t}", START)

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def loop(self, kalshi=None, espn=None, minutes=0.0):
        kalshi = kalshi or FakeKalshi()
        eng = Engine(self.s, self.st, data_client=kalshi, llm=False)
        eng.betting_ok = True
        games = lg.LiveGames(self.s, self.st, eng, fetch=espn or FakeESPN(), now_fn=lambda: NOW)
        return games.run(minutes, sleep=lambda s: None), kalshi

    def bets(self):
        return self.st.q("SELECT ticker, side, price, stake FROM bets")


class TestModel(unittest.TestCase):
    def test_fraction_left(self):
        self.assertAlmostEqual(lg.fraction_left("nba", 4, "2:00"), 120 / 2880)
        self.assertAlmostEqual(lg.fraction_left("nba", 1, "12:00"), 1.0)
        self.assertAlmostEqual(lg.fraction_left("nfl", 3, "15:00"), 0.5)
        self.assertAlmostEqual(lg.fraction_left("nhl", 2, "10:00"), 0.5)
        self.assertAlmostEqual(lg.fraction_left("mlb", 5, None, "Top 5th"), 10 / 18)
        self.assertAlmostEqual(lg.fraction_left("mlb", 5, None, "Bottom 5th"), 9 / 18)
        self.assertGreater(lg.fraction_left("nba", 5, "3:00"), 0)          # overtime sliver
        self.assertIsNone(lg.fraction_left("nba", 4, "x:y"))
        self.assertIsNone(lg.fraction_left("cricket", 1, "1:00"))

    def test_win_prob(self):
        self.assertAlmostEqual(lg.win_prob(0, 1.0, 0.6, 13), 0.6, places=6)  # tip-off = prior
        self.assertGreater(lg.win_prob(15, 0.05, 0.4, 13), 0.99)            # big late lead
        self.assertLess(lg.win_prob(-15, 0.05, 0.6, 13), 0.01)
        self.assertGreater(lg.win_prob(5, 0.1, 0.5, 13), lg.win_prob(5, 0.6, 0.5, 13))
        self.assertEqual(lg.win_prob(1, 0, 0.3, 13), 1.0)

    def test_match_team(self):
        ev = lg.scoreboard("nba", FakeESPN())[0]
        self.assertEqual(lg.match_team("Boston", "BOS", ev)["abbreviation"], "BOS")
        self.assertEqual(lg.match_team("New York", "NYK", ev)["abbreviation"], "NYK")
        self.assertIsNone(lg.match_team("Denver", "DEN", ev))


class TestLoop(Base):
    def test_places_paper_bet_on_live_edge(self):
        out, _ = self.loop()
        self.assertEqual(out["live_games"], 1)
        self.assertEqual(out["live_bets"], 1, out)
        bet = self.bets()[0]
        self.assertEqual((bet["ticker"], bet["side"]), (EVENT + "-BOS", "yes"))
        self.assertLessEqual(bet["stake"], self.s.live_max_per_bet + 0.01)
        snaps = self.st.q("SELECT * FROM live_game_snaps ORDER BY ticker")
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[0]["is_home"], 1)
        self.assertEqual(snaps[0]["lead"], 15)
        self.assertEqual(self.st.get_kv("live_games_health")["live_bets"], 1)

    def test_one_bet_per_game(self):
        self.loop()
        self.loop()
        self.assertEqual(len(self.bets()), 1)

    def test_paper_only(self):
        self.s.mode = "demo"
        with mock.patch.object(lg, "scoreboard") as sb:
            out, _ = self.loop()
        sb.assert_not_called()
        self.assertIn("live game bets are paper-only", out["skipped"])
        self.assertEqual(self.bets(), [])

    def test_no_live_games_returns_after_one_check(self):
        espn = FakeESPN(scoreboard=espn_payload(state="pre"))
        out, _ = self.loop(espn=espn, minutes=8)
        self.assertEqual((out["live_games"], out["polls"]), (0, 1))

    def test_final_moments_blocked(self):
        out, _ = self.loop(espn=FakeESPN(scoreboard=espn_payload(clock="0:30")))
        self.assertIn("final moments of the game", out["skipped"])
        self.assertEqual(self.bets(), [])

    def test_disagreement_with_espn_blocked(self):
        out, _ = self.loop(espn=FakeESPN(home_wp=0.55))
        self.assertIn("score model and ESPN disagree (data check)", out["skipped"])
        self.assertEqual(self.bets(), [])

    def test_no_edge_no_bet(self):
        out, _ = self.loop(kalshi=FakeKalshi(bos_ask=0.985))
        self.assertEqual(self.bets(), [])
        self.assertGreater(out["priced"], 0)

    def test_needs_pre_game_anchor(self):
        self.st.x("DELETE FROM market_snapshots")
        out, _ = self.loop()
        self.assertIn("no pre-game price to anchor on", out["skipped"])

    def test_polls_repeat_until_deadline(self):
        clock = [NOW]
        eng = Engine(self.s, self.st, data_client=FakeKalshi(bos_ask=0.985), llm=False)
        eng.betting_ok = True
        games = lg.LiveGames(self.s, self.st, eng, fetch=FakeESPN(), now_fn=lambda: clock[0])

        def sleep(sec):
            clock[0] += sec
        out = games.run(2, sleep=sleep)
        self.assertEqual(out["polls"], 6)            # every 20 s for 2 minutes
        # one saved snapshot per market per minute, not per poll
        self.assertEqual(self.st.q("SELECT COUNT(*) AS n FROM live_game_snaps")[0]["n"], 4)

    def test_paused_betting_blocks(self):
        kalshi = FakeKalshi()
        eng = Engine(self.s, self.st, data_client=kalshi, llm=False)
        eng.betting_ok = False
        out = lg.LiveGames(self.s, self.st, eng, fetch=FakeESPN(), now_fn=lambda: NOW).run(0)
        self.assertIn("betting paused", out["skipped"])
        self.assertEqual(self.bets(), [])


class TestLearning(Base):
    def seed(self, n_games, sigma_true, start=0):
        import random
        rnd = random.Random(3 + start)
        lg.ensure_table(self.st)
        for g in range(n_games):
            final = rnd.gauss(0, sigma_true)
            for i, frac in enumerate((0.75, 0.5, 0.25, 0.1)):
                lead = final * (1 - frac) + rnd.gauss(0, 0.3)
                self.st.x("""INSERT INTO live_game_snaps (ticker, ts, sport, lead, frac_left, prior,
                             model_p, espn_p, market_p, outcome, settled_ts)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (f"G{start + g}", 1000 * (start + g) + i, "nba", lead, frac, 0.5,
                           0.5, None, 0.5, int(final > 0), 1))

    def test_learns_sigma_that_beats_default(self):
        self.seed(120, sigma_true=7.0)
        fit = lg.learn(self.st)["nba"]
        self.assertTrue(fit["accepted"], fit)
        self.assertLess(fit["sigma"], 13.0)
        self.assertEqual(lg.sigma_for(self.st, "nba"), fit["sigma"])

    def test_needs_enough_games(self):
        self.seed(10, sigma_true=7.0)
        fit = lg.learn(self.st)["nba"]
        self.assertFalse(fit["accepted"])
        self.assertEqual(lg.sigma_for(self.st, "nba"), 13.0)

    def test_grading_labels_finished_games(self):
        self.loop()
        kalshi = FakeKalshi()
        kalshi.results = {EVENT + "-BOS": "yes"}
        self.assertEqual(lg.grade(self.st, kalshi), 1)
        rows = {r["ticker"]: r["outcome"] for r in self.st.q("SELECT ticker, outcome FROM live_game_snaps")}
        self.assertEqual(rows[EVENT + "-BOS"], 1)
        self.assertIsNone(rows[EVENT + "-NYK"])

    def test_config_validation(self):
        with mock.patch.dict(os.environ, {"LIVE_POLL_SECONDS": "1"}):
            with self.assertRaisesRegex(ValueError, "LIVE_POLL_SECONDS"):
                Settings().validate()


if __name__ == "__main__":
    unittest.main()
