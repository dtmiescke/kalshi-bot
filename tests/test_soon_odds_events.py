import time
import unittest

from bot.engine import _rotating_event_batch, soon_odds_events


def ev(ticker, series, hours, now):
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + hours * 3600))
    return {"event_ticker": ticker, "series_ticker": series,
            "markets": [{"status": "active", "occurrence_datetime": start}]}


class TestSoonOddsEvents(unittest.TestCase):
    def test_soon_games_in_odds_leagues_only(self):
        now = time.time()
        by = {"KXNBAGAME": [ev("A", "KXNBAGAME", 5, now), ev("B", "KXNBAGAME", 80, now),
                            ev("C", "KXNBAGAME", 1, now)],
              "KXOTHER": [ev("D", "KXOTHER", 2, now)]}
        self.assertEqual(soon_odds_events(by, ["KXNBAGAME"], now), ["C", "A"])


    def test_cap_keeps_nearest(self):
        now = time.time()
        by = {"S": [ev(f"E{i}", "S", i + 1, now) for i in range(10)]}
        self.assertEqual(soon_odds_events(by, ["S"], now, cap=3), ["E0", "E1", "E2"])


    def test_soon_events_survive_a_tiny_batch(self):
        now = time.time()
        events = [ev(f"Z{i}", "Z", 200, now) for i in range(50)] + [ev("SOON", "S", 6, now)]
        out, _ = _rotating_event_batch(events, "", 5, held={"SOON"})
        self.assertIn("SOON", [e["event_ticker"] for e in out])
