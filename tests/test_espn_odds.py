import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from bot import espn_odds
from bot.odds import consensus
from bot.storage import Store


def game(gid="1", state="pre", home="Washington Wizards", away="Charlotte Hornets", ml=None, draw=None, date="2026-10-31T00:00Z"):
    odds = {"provider": {"name": "DraftKings"}, "moneyline": ml if ml is not None else {
        "home": {"close": {"odds": "-120"}, "open": {"odds": "-105"}},
        "away": {"close": {"odds": "+100"}, "open": {"odds": "-115"}}}}
    if draw:
        odds["moneyline"]["draw"] = {"close": {"odds": draw}}
    return {"id": gid, "date": date, "status": {"type": {"state": state}},
            "competitions": [{"competitors": [{"homeAway": "home", "team": {"displayName": home}},
                                              {"homeAway": "away", "team": {"displayName": away}}], "odds": [odds]}]}


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class TestEspnOdds(unittest.TestCase):
    def test_a_two_way_line_becomes_an_odds_api_event_the_consensus_can_read(self):
        ev = espn_odds.event_from_game(game())
        self.assertEqual(ev["id"], "espn:1")
        probs = consensus(ev)
        self.assertAlmostEqual(sum(p for p, _, _ in probs.values()), 1.0, places=6)    # vig removed
        self.assertGreater(probs["Washington Wizards"][0], probs["Charlotte Hornets"][0])   # -120 vs +100
        self.assertEqual(probs["Washington Wizards"][1], 1)                                  # one book

    def test_live_finished_or_priceless_games_are_skipped(self):
        self.assertIsNone(espn_odds.event_from_game(game(state="in")))
        self.assertIsNone(espn_odds.event_from_game(game(state="post")))
        self.assertIsNone(espn_odds.event_from_game(game(ml={"home": {"close": {"odds": "-120"}}})))
        self.assertIsNone(espn_odds.event_from_game(game(ml={"home": {"close": {"odds": "N/A"}}, "away": {}})))

    def test_soccer_needs_its_draw_price_and_labels_it_draw(self):
        self.assertIsNone(espn_odds.event_from_game(game(), three_way=True))
        ev = espn_odds.event_from_game(game(home="Chelsea", away="Manchester United", draw="+295"), three_way=True)
        names = [o["name"] for o in ev["bookmakers"][0]["markets"][0]["outcomes"]]
        self.assertEqual(names, ["Chelsea", "Manchester United", "Draw"])
        self.assertAlmostEqual(sum(p for p, _, _ in consensus(ev).values()), 1.0, places=6)

    def test_the_opening_price_is_used_only_when_there_is_no_current_one(self):
        ev = espn_odds.event_from_game(game(ml={"home": {"open": {"odds": "-150"}}, "away": {"open": {"odds": "+130"}}}))
        self.assertEqual([o["price"] for o in ev["bookmakers"][0]["markets"][0]["outcomes"]], [-150.0, 130.0])

    def test_fetch_asks_for_a_date_range_and_unknown_sports_cost_nothing(self):
        get = MagicMock(return_value=Resp({"events": [game("7"), game("8", state="post")]}))
        out = espn_odds.fetch("basketball_nba", get=get, now=1_793_300_000)
        self.assertEqual([e["id"] for e in out], ["espn:7"])           # the same game on every day's page counts once
        self.assertIn("basketball/nba", get.call_args[0][0])
        days = [c[1]["params"]["dates"] for c in get.call_args_list]
        self.assertEqual(len(days), 4)                                 # yesterday through two days ahead, never a range
        self.assertTrue(all(len(d) == 8 and d.isdigit() for d in days))
        self.assertEqual(espn_odds.fetch("curling_world_cup", get=get), [])
        self.assertEqual(get.call_count, 4)
        with self.assertRaises(RuntimeError):
            espn_odds.fetch("basketball_nba", get=MagicMock(return_value=Resp([])))


class TestEngineFill(unittest.TestCase):
    def engine(self):
        from bot.engine import Engine
        eng = Engine.__new__(Engine)
        eng.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        eng.odds_max_age = 60
        return eng

    def test_fills_when_the_odds_api_data_is_stale_and_not_when_fresh(self):
        eng, now = self.engine(), time.time()
        resp = Resp({"events": [game("7")]})
        with patch("bot.espn_odds.requests.get", return_value=resp) as get:
            eng.store.set_kv("odds_last_basketball_nba", now - 60)          # fresh Odds API data: never double count
            eng.espn_fill("basketball_nba", now)
            self.assertEqual(get.call_count, 0)
            eng.store.set_kv("odds_last_basketball_nba", now - 4 * 3600)
            eng.espn_fill("basketball_nba", now)
            self.assertEqual(get.call_count, 4)
            self.assertEqual(eng.store.get_kv("odds_events_basketball_nba")[0]["id"], "espn:7")
            self.assertEqual(eng.store.get_kv("espn_odds_health_basketball_nba")["status"], "ok")
            eng.espn_fill("basketball_nba", now + 60)                         # throttled
            self.assertEqual(get.call_count, 4)
        rows = eng.store.q("SELECT team, n_books FROM odds_snapshots WHERE sport='basketball_nba'")
        self.assertEqual(len(rows), 2)
        self.assertEqual(eng.store.get_kv("odds_last_basketball_nba"), now - 4 * 3600)   # does not hold back an Odds API refresh

    def test_a_failed_request_backs_off_and_keeps_the_cache(self):
        eng, now = self.engine(), time.time()
        eng.store.set_kv("odds_events_basketball_nba", [{"id": "keep"}])
        with patch("bot.espn_odds.requests.get", side_effect=RuntimeError("boom")) as get:
            eng.espn_fill("basketball_nba", now)
            eng.espn_fill("basketball_nba", now + 30)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(eng.store.get_kv("odds_events_basketball_nba"), [{"id": "keep"}])

    def test_the_switch_turns_it_off(self):
        eng = self.engine()
        with patch.dict(os.environ, {"ESPN_ODDS": "no"}), patch("bot.espn_odds.requests.get") as get:
            eng.espn_fill("basketball_nba", time.time())
        self.assertEqual(get.call_count, 0)


if __name__ == "__main__":
    unittest.main()
