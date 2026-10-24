"""Regression tests for the eight-worker sports scanner fleet."""
import tempfile
import unittest
from pathlib import Path

from bot.scanner_fleet import SCANNER_COUNT, scan_events
from bot.storage import Store


class TestScannerFleet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "bot.sqlite"))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_scans_unique_events_once_and_prioritizes_held_event(self):
        events = {
            "KXNFLGAME": [
                {"event_ticker": "EVT-A", "markets": [
                    {"ticker": "M1", "status": "active", "yes_bid": 40, "yes_ask": 45}]},
                {"event_ticker": "EVT-B", "markets": [
                    {"ticker": "M2", "status": "open", "yes_bid": 0.4, "yes_ask": 0.5}]},
            ],
            "KXNCAAFGAME": [
                {"event_ticker": "EVT-A", "markets": [
                    {"ticker": "M1", "status": "active", "yes_bid": 40, "yes_ask": 45}]},
            ],
        }
        report = scan_events(self.store, events, held={"EVT-B"}, now=123)
        self.assertEqual(report["workers"], SCANNER_COUNT)
        self.assertEqual(report["unique_events"], 2)
        self.assertEqual(report["events_scanned"], 2)
        self.assertEqual(report["duplicate_event_records_skipped"], 1)
        self.assertEqual(report["candidate_events"], 2)
        self.assertEqual(report["held_events_seen"], 1)
        self.assertEqual(len(report["worker_stats"]), SCANNER_COUNT)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(self.store.get_kv("sports_scanner_fleet_health")["events_scanned"], 2)
        # Scanner hints never become trade records.
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM bets")[0]["n"], 0)

    def test_missing_or_invalid_quotes_do_not_become_candidates(self):
        events = {"KXNFLGAME": [
            {"event_ticker": "EVT-C", "markets": [
                {"ticker": "M3", "status": "active", "yes_bid": None, "yes_ask": None}]},
            {"event_ticker": "EVT-D", "markets": [
                {"ticker": "M4", "status": "closed", "yes_bid": 0.4, "yes_ask": 0.5}]},
        ]}
        report = scan_events(self.store, events, now=124)
        self.assertEqual(report["unique_events"], 2)
        self.assertEqual(report["candidate_events"], 0)
        self.assertEqual(report["quoteable_markets"], 0)

    def test_duplicate_ids_keep_richer_event_representation(self):
        events = {
            "KXNFLGAME": [{"event_ticker": "EVT-A", "markets": []}],
            "KXNCAAFGAME": [{"event_ticker": "EVT-A", "markets": [
                {"ticker": "M1", "status": "active", "yes_bid": 0.3, "yes_ask": 0.4}]}],
        }
        report = scan_events(self.store, events, now=125)
        self.assertEqual(report["unique_events"], 1)
        self.assertEqual(report["duplicate_event_records_skipped"], 1)
        self.assertEqual(report["candidate_events"], 1)


if __name__ == "__main__":
    unittest.main()
