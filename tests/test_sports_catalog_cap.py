"""The catalog listing is capped, and the leagues the bot has sportsbook odds for fell outside it: no games
scanned, odds never refreshed, every sports market skipped. They are now fetched directly, and their odds
stay fresh even when a scan's rotating batch holds none of their games."""
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import engine as engine_mod
from bot.config import Settings
from bot.engine import Engine, top_up_known_leagues
from bot.storage import Store


def game(series, n, hours=6):
    from datetime import datetime, timedelta, timezone
    t = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"event_ticker": f"{series}-26OCT30G{n}", "series_ticker": series, "title": f"Game {n}",
            "markets": [{"ticker": f"{series}-26OCT30G{n}-A", "status": "active", "occurrence_datetime": t,
                         "expected_expiration_time": t, "close_time": t, "yes_bid_dollars": "0.40",
                         "yes_ask_dollars": "0.42"}]}


class FakeData:
    def __init__(self, listed, direct):
        self.listed, self.direct, self.asked = listed, direct, []

    def sports_events(self, series):
        self.asked.append(series)
        if self.direct.get(series) == "boom":
            raise RuntimeError("429")
        return [dict(e) for e in self.direct.get(series, [])]


class TestTopUp(unittest.TestCase):
    def test_a_league_missing_from_the_capped_listing_is_fetched_directly(self):
        by, smap = {"KXTENNIS": [game("KXTENNIS", 1)]}, {"KXTENNIS": "tennis"}
        data = FakeData(None, {"KXNBAGAME": [game("KXNBAGAME", 1), game("KXNBAGAME", 2)]})
        added = top_up_known_leagues(data, by, smap, known={"KXNBAGAME": "basketball_nba", "KXNHLGAME": "icehockey_nhl"})
        self.assertEqual(added, ["KXNBAGAME"])                       # NHL had nothing open: nothing to add
        self.assertEqual(len(by["KXNBAGAME"]), 2)
        self.assertEqual(smap["KXNBAGAME"], "basketball_nba")        # mapped to its odds key
        self.assertEqual(by["KXNBAGAME"][0]["series_ticker"], "KXNBAGAME")
        self.assertIn("KXTENNIS", by)                                 # what the listing did find is untouched

    def test_a_league_the_listing_already_has_is_not_refetched(self):
        by, smap = {"KXNBAGAME": [game("KXNBAGAME", 1)]}, {}
        data = FakeData(None, {"KXNBAGAME": [game("KXNBAGAME", 9)]})
        self.assertEqual(top_up_known_leagues(data, by, smap, known={"KXNBAGAME": "basketball_nba"}), [])
        self.assertEqual(data.asked, [])

    def test_an_api_error_skips_that_league_and_keeps_going(self):
        by, smap = {}, {}
        data = FakeData(None, {"KXNBAGAME": "boom", "KXNHLGAME": [game("KXNHLGAME", 1)]})
        added = top_up_known_leagues(data, by, smap, known={"KXNBAGAME": "basketball_nba", "KXNHLGAME": "icehockey_nhl"})
        self.assertEqual(added, ["KXNHLGAME"])

    def test_a_manual_selection_is_respected(self):
        by, smap = {}, {}
        data = FakeData(None, {"KXNBAGAME": [game("KXNBAGAME", 1)], "KXNHLGAME": [game("KXNHLGAME", 1)]})
        added = top_up_known_leagues(data, by, smap, only={"KXNHLGAME"},
                                     known={"KXNBAGAME": "basketball_nba", "KXNHLGAME": "icehockey_nhl"})
        self.assertEqual(added, ["KXNHLGAME"])
        self.assertEqual(data.asked, ["KXNHLGAME"])


class TestRunKeepsOddsFresh(unittest.TestCase):
    def test_odds_refresh_even_when_the_batch_holds_none_of_the_leagues_games(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        settings = Settings()
        settings.sports_mode = "all"
        nba = [game("KXNBAGAME", i) for i in range(3)]
        many = [game(f"KXOTHER{i}", i, hours=0.5) for i in range(40)]      # imminent games fill the whole batch

        class Data:
            def sports_series(self_):
                return {"KXNBAGAME": {}, **{f"KXOTHER{i}": {} for i in range(40)}}

            def sports_events_all(self_):
                return list(many)                                            # the capped listing never reached the NBA

            def sports_events(self_, series):
                return [dict(e) for e in (nba if series == "KXNBAGAME" else [])]

        eng = Engine(settings, store, data_client=Data())
        refreshed = []
        with patch.object(Engine, "refresh_odds", lambda self, sport, nxt=None: refreshed.append(sport)), \
                patch("bot.sports_fleet.scan_fleet", lambda events_by_series, now=None, workers=8: (events_by_series, {"ok": True}, [])), \
                patch.dict(os.environ, {"SPORTS_INTEL_EVENT_SEARCH": "no", "SPORTS_INTEL_ENABLED": "no"}):
            try:
                eng.run(sports=True, event_budget=5)
            except Exception:
                pass                                                         # what happens after the odds step is not under test
        self.assertIn("basketball_nba", refreshed)


if __name__ == "__main__":
    unittest.main()
