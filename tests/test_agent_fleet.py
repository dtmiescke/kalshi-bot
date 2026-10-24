"""Regression tests for the coordinated scanner and maintenance agents."""
import tempfile
import unittest
from pathlib import Path

from bot.agent_fleet import (
    SCANNER_COUNT, code_nerd_preflight, janitor_deduplicate,
    janitor_retention, scan_events,
)
from bot.storage import Store


class TestAgentFleet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "bot.sqlite"))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_eight_scanners_deduplicate_event_ids_and_do_not_write_bets(self):
        events = {
            "KXNFLGAME": [
                {"event_ticker": "EVT-A", "markets": [
                    {"ticker": "M1", "status": "active", "yes_bid": 40, "yes_ask": 45}]},
                {"event_ticker": "EVT-B", "markets": [
                    {"ticker": "M2", "status": "active", "yes_bid": 0.4, "yes_ask": 0.5}]},
            ],
            "KXNCAAFGAME": [
                {"event_ticker": "EVT-A", "markets": [
                    {"ticker": "M1", "status": "active", "yes_bid": 40, "yes_ask": 45}]},
            ],
        }
        report = scan_events(self.store, events, held={"EVT-B"}, now=123)
        self.assertEqual(report["workers"], SCANNER_COUNT)
        self.assertEqual(report["unique_events"], 2)
        self.assertEqual(report["assigned_events"], 2)
        self.assertEqual(report["duplicate_event_records_skipped"], 1)
        self.assertEqual(report["candidate_events"], 2)
        self.assertEqual(report["held_events_seen"], 1)
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM bets")[0]["n"], 0)
        self.assertEqual(self.store.get_kv("agent_fleet_live_scan")["status"], "ok")

    def test_janitor_removes_only_exact_duplicate_raw_observations(self):
        row = {"ts": 100, "ticker": "M1", "event_ticker": "EVT-A",
               "series": "KXNFLGAME", "title": "Example", "team": "A",
               "yes_bid": 0.4, "yes_ask": 0.5, "last_price": 0.45,
               "volume": 10, "open_interest": 3, "close_time": "2030-01-01",
               "status": "active"}
        self.store.add_market_snapshot(row)
        self.store.add_market_snapshot(row)
        changed = dict(row, ts=101, yes_bid=0.41)
        self.store.add_market_snapshot(changed)
        report = janitor_deduplicate(self.store)
        self.assertEqual(report["deleted_exact_duplicates"]["market_snapshots"], 1)
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM market_snapshots")[0]["n"], 2)

    def test_code_nerd_preflight_checks_schema_and_database(self):
        report = code_nerd_preflight(self.store, root=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["python_compile_ok"])
        self.assertEqual(self.store.get_kv("fleet_safety_hold"), None)

    def test_retention_janitor_is_daily_and_does_not_prune_bets(self):
        self.store.x("INSERT INTO bets (ts,mode,ticker,event_ticker,series,team,side,price,contracts,stake,fee,model_prob,edge,confidence,signals_json,status) VALUES (1,'paper','M','E','KXNFLGAME','A','yes',0.4,1,0.4,0,0.6,0.2,0.8,'{}','open')")
        first = janitor_retention(self.store, now=200000)
        second = janitor_retention(self.store, now=200001)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM bets")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
