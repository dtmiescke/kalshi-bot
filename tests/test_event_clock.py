"""Event-clock scheduling tests: urgency plus starvation-free rotation."""
import time
import unittest
from datetime import datetime, timezone

from bot.engine import _rotating_event_batch
from bot.event_clock import event_start_ts


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class TestEventClock(unittest.TestCase):
    def test_occurrence_time_event_schedule_and_expiration_fallback(self):
        now = time.time()
        self.assertAlmostEqual(event_start_ts({"markets": [{
            "status": "active", "occurrence_datetime": iso(now + 300)}]}),
            now + 300, delta=1)
        self.assertAlmostEqual(event_start_ts({"markets": [{
            "status": "open", "expected_expiration_time": iso(now + 600)}]}),
            now + 600, delta=1)
        self.assertAlmostEqual(event_start_ts({"start_time": iso(now + 900), "markets": []}),
            now + 900, delta=1)
        self.assertAlmostEqual(event_start_ts({"start_time": "bad", "markets": [{
            "status": "open", "expected_expiration_time": iso(now + 1200)}]}),
            now + 1200, delta=1)

    def test_near_start_event_is_prioritized_and_duplicates_removed(self):
        now = time.time()
        events = [
            {"event_ticker": "A-FAR", "markets": [{"status": "open",
             "occurrence_datetime": iso(now + 10 * 3600)}]},
            {"event_ticker": "B-UNTIMED", "markets": []},
            {"event_ticker": "Z-SOON", "markets": [{"status": "active",
             "occurrence_datetime": iso(now + 5 * 60)}]},
            {"event_ticker": "C-DUP", "markets": []},
            {"event_ticker": "C-DUP", "markets": []},
        ]
        batch, _ = _rotating_event_batch(events, "", budget=4)
        tickers = [event["event_ticker"] for event in batch]
        self.assertIn("Z-SOON", tickers)
        self.assertEqual(tickers.count("C-DUP"), 1)
        self.assertEqual(len(tickers), 4)

    def test_held_events_remain_and_recently_started_games_get_priority(self):
        now = time.time()
        events = [
            {"event_ticker": "HELD", "markets": []},
            {"event_ticker": "STARTED", "markets": [{"status": "active",
             "occurrence_datetime": iso(now - 15 * 60)}]},
            {"event_ticker": "LATER", "markets": [{"status": "open",
             "occurrence_datetime": iso(now + 8 * 3600)}]},
        ]
        batch, _ = _rotating_event_batch(events, "", budget=1, held={"HELD"})
        tickers = [event["event_ticker"] for event in batch]
        self.assertIn("HELD", tickers)
        self.assertIn("STARTED", tickers)


if __name__ == "__main__":
    unittest.main()
