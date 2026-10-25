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
        # Each specialist independently reviews the full canonical event universe.
        self.assertTrue(all(scanner["events"] == 24 for scanner in summary["scanners"]))
        self.assertEqual(len({scanner["role"] for scanner in summary["scanners"]}), 8)
        roles = {scanner["role"]: scanner for scanner in summary["scanners"]}
        self.assertEqual(roles["quote_quality"]["priced_markets"], 24)
        self.assertEqual(summary["active_markets"], 24)
        self.assertEqual(summary["active_markets_with_quotes"], 24)
        self.assertEqual(summary["events_with_start_time"], 0)
        self.assertEqual(roles["schedule_and_status"]["missing_start_times"], 24)
        self.assertTrue(all(row["execution_eligible"] is False for row in live))
        self.assertEqual(len({row["ticker"] for row in live}), len(live))


    def test_kalshi_dollar_quotes_and_occurrence_time_are_counted(self):
        _, summary, _ = scan_fleet({"KXNBAGAME": [{
            "event_ticker": "EVT-CLOCK",
            "markets": [{
                "ticker": "MKT-DOLLARS", "status": "active",
                "yes_bid_dollars": "0.41", "yes_ask_dollars": "0.44",
                "occurrence_datetime": "2026-10-25T13:00:00Z",
            }],
        }]}, now=123, workers=8)
        self.assertEqual(summary["events_with_start_time"], 1)
        self.assertEqual(summary["events_missing_start_time"], 0)
        self.assertEqual(summary["active_markets_with_quotes"], 1)
        self.assertEqual(summary["active_markets_missing_quotes"], 0)
        roles = {scanner["role"]: scanner for scanner in summary["scanners"]}
        self.assertEqual(roles["quote_quality"]["priced_markets"], 1)

    def test_malformed_records_are_skipped_without_crashing_other_scanners(self):
        normalized = normalize_events({"KXNFLGAME": [
            None,
            {"event_ticker": "EVT", "markets": [
                None, {"ticker": "OK", "status": "active", "yes_bid": 40, "yes_ask": 45}]},
        ]})
        self.assertEqual(len(normalized["KXNFLGAME"]), 1)
        self.assertEqual([m["ticker"] for m in normalized["KXNFLGAME"][0]["markets"]], ["OK"])
        normalized, summary, live = scan_fleet(normalized, now=126, workers=8)
        self.assertEqual(summary["events_unique"], 1)
        self.assertTrue(summary["ok"])

    def test_empty_or_missing_tickers_are_not_duplicate_market_records(self):
        normalized = normalize_events({"KXNBAGAME": [
            {"event_ticker": "", "markets": [{"ticker": "BAD"}]},
            {"event_ticker": "EVT", "markets": [{"ticker": ""}, {"ticker": "OK"}]},
        ]})
        self.assertEqual(len(normalized["KXNBAGAME"]), 1)
        self.assertEqual([m["ticker"] for m in normalized["KXNBAGAME"][0]["markets"]], ["OK"])


if __name__ == "__main__":
    unittest.main()
