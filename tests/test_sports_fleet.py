"""Tests for deduplicated eight-scanner sports coordination."""
import unittest
from bot.sports_fleet import normalize_events, scan_fleet


class TestSportsFleet(unittest.TestCase):
    def test_duplicate_events_and_markets_are_collapsed(self):
        a = {"event_ticker": "EVT1", "series_ticker": "KXNFLGAME",
             "status": "live", "markets": [
                 {"ticker": "MKT1", "status": "active", "yes_bid": 40, "yes_ask": 42}]}
        duplicate = {"event_ticker": "EVT1", "series_ticker": "KXNFLGAME",
                     "title": "richer event", "markets": [
                         {"ticker": "MKT1", "status": "active", "yes_bid": 40,
                          "yes_ask": 42, "volume": 500},
                         {"ticker": "MKT2", "status": "open"}]}
        normalized = normalize_events({"KXNFLGAME": [a, duplicate]})
        events = normalized["KXNFLGAME"]
        self.assertEqual(len(events), 1)
        self.assertEqual({m["ticker"] for m in events[0]["markets"]}, {"MKT1", "MKT2"})
        self.assertEqual(next(m for m in events[0]["markets"] if m["ticker"] == "MKT1")["volume"], 500)

    def test_eight_scanners_partition_events_and_only_observe_live_markets(self):
        events = {"KXNFLGAME": [
            {"event_ticker": f"EVT{i}", "status": "live" if i % 2 == 0 else "scheduled",
             "markets": [{"ticker": f"MKT{i}", "status": "active",
                          "yes_bid": 40, "yes_ask": 45}]}
            for i in range(24)
        ]}
        normalized, summary, live = scan_fleet(events, now=123, workers=8)
        self.assertEqual(summary["scanner_count"], 8)
        self.assertEqual(summary["events_unique"], 24)
        self.assertEqual(summary["markets_unique"], 24)
        self.assertEqual(summary["live_markets_observed"], 12)
        self.assertTrue(all(row["execution_eligible"] is False for row in live))
        self.assertEqual(len({row["ticker"] for row in live}), len(live))

    def test_empty_or_missing_tickers_are_not_duplicate_market_records(self):
        normalized = normalize_events({"KXNBAGAME": [
            {"event_ticker": "", "markets": [{"ticker": "BAD"}]},
            {"event_ticker": "EVT", "markets": [{"ticker": ""}, {"ticker": "OK"}]},
        ]})
        self.assertEqual(len(normalized["KXNBAGAME"]), 1)
        self.assertEqual([m["ticker"] for m in normalized["KXNBAGAME"][0]["markets"]], ["OK"])


if __name__ == "__main__":
    unittest.main()
