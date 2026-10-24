"""Tests for the maintenance agents and fail-closed Nerd watchdog."""
import os
import tempfile
import time
import unittest

from bot.storage import Store
from bot.maintenance import janitor_dedupe, run_janitors
from bot.nerd import inspect_code, inspect_store


class TestAgentMaintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bot.sqlite")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_janitor_deduplicates_raw_observations_and_adds_unique_indexes(self):
        now = int(time.time())
        with self.store.lock:
            self.store.db.executemany(
                "INSERT INTO market_snapshots (ts,ticker) VALUES (?,?)",
                [(now, "KXTEST"), (now, "KXTEST")])
            self.store.db.executemany(
                "INSERT INTO odds_snapshots (ts,event_id,team) VALUES (?,?,?)",
                [(now, "event-1", "home"), (now, "event-1", "home")])
            self.store.db.executemany(
                "INSERT INTO signal_readings (ts,ticker,signal) VALUES (?,?,?)",
                [(now, "KXTEST", "test"), (now, "KXTEST", "test")])
            self.store.db.commit()

        result = janitor_dedupe(self.store)
        self.assertEqual(result["removed"]["market_snapshots"]["duplicates"], 1)
        self.assertEqual(result["removed"]["odds_snapshots"]["duplicates"], 1)
        self.assertEqual(result["removed"]["signal_readings"]["duplicates"], 1)
        self.assertEqual(self.store.q("SELECT COUNT(*) AS n FROM market_snapshots")[0]["n"], 1)
        with self.assertRaises(Exception):
            self.store.db.execute(
                "INSERT INTO market_snapshots (ts,ticker) VALUES (?,?)", (now, "KXTEST"))

    def test_janitor_dedupe_is_idempotent(self):
        now = int(time.time())
        self.store.db.executemany(
            "INSERT INTO market_snapshots (ts,ticker) VALUES (?,?)",
            [(now, "IDEMPOTENT"), (now, "IDEMPOTENT")])
        self.store.db.commit()
        first = janitor_dedupe(self.store, now=now)
        second = janitor_dedupe(self.store, now=now)
        self.assertEqual(first["removed"]["market_snapshots"]["duplicates"], 1)
        self.assertEqual(second["removed"]["market_snapshots"]["duplicates"], 0)
        self.assertEqual(self.store.q(
            "SELECT COUNT(*) n FROM market_snapshots WHERE ticker=?",
            ("IDEMPOTENT",))[0]["n"], 1)

    def test_raw_writers_ignore_duplicate_keys_after_janitor_indexes_exist(self):
        now = int(time.time())
        market = {"ts": now, "ticker": "KXTEST", "event_ticker": "EVT",
                  "series": "KXNFLGAME", "title": "Test", "team": "A",
                  "yes_bid": .4, "yes_ask": .5, "last_price": .45,
                  "volume": 10, "open_interest": 2, "close_time": "2030-01-01",
                  "status": "active"}
        odds = {"ts": now, "sport": "americanfootball_nfl", "event_id": "EVT",
                "commence_time": "2030-01-01", "home": "A", "away": "B",
                "team": "A", "fair_prob": .6, "n_books": 4, "raw": {}}
        janitor_dedupe(self.store)
        self.store.add_market_snapshot(market)
        self.store.add_market_snapshot(dict(market, yes_bid=.41))
        self.store.add_odds(odds)
        self.store.add_odds(dict(odds, fair_prob=.61))
        self.store.add_signal(now, "KXTEST", "book_consensus", .6, .8, {})
        self.store.add_signal(now, "KXTEST", "book_consensus", .61, .8, {})
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM market_snapshots")[0]["n"], 1)
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM odds_snapshots")[0]["n"], 1)
        self.assertEqual(self.store.q("SELECT COUNT(*) n FROM signal_readings")[0]["n"], 1)

    def test_retention_janitor_preserves_bets_and_learnings(self):
        now = int(time.time())
        with self.store.lock:
            self.store.db.execute(
                "INSERT INTO market_snapshots (ts,ticker) VALUES (?,?)",
                (now - 20 * 86400, "OLD"))
            self.store.db.execute("INSERT INTO lessons (ts,model,text) VALUES (?,?,?)",
                                  (now - 365 * 86400, "test", "keep learned lesson"))
            self.store.db.execute(
                "INSERT INTO bets (ts,mode,ticker,side,price,contracts,stake,fee) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (now - 365 * 86400, "paper", "KXTEST", "yes", .5, 1, .5, 0))
            self.store.db.commit()
        result = run_janitors(self.store, now=now, force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.q("SELECT COUNT(*) AS n FROM market_snapshots")[0]["n"], 0)
        self.assertEqual(self.store.q("SELECT COUNT(*) AS n FROM lessons")[0]["n"], 1)
        self.assertEqual(self.store.q("SELECT COUNT(*) AS n FROM bets")[0]["n"], 1)

    def test_nerd_checks_required_source_structure(self):
        report = inspect_code()
        self.assertTrue(report["ok"])
        self.assertGreaterEqual(report["required_paths"], 10)
        self.assertEqual(report["missing_paths"], [])

    def test_nerd_blocks_when_critical_source_file_is_missing(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(RuntimeError, "required source structure missing"):
                inspect_code(root=empty)

    def test_nerd_watchdog_reports_healthy_database(self):
        report = inspect_store(self.store)
        self.assertTrue(report["ok"])
        self.assertEqual(report["checks"]["quick_check"], ["ok"])

    def test_janitors_respect_six_hour_cadence(self):
        now = int(time.time())
        self.store.set_kv("janitors_last_run", now)
        result = run_janitors(self.store, now=now + 60, force=False)
        self.assertEqual(result["skipped"], "not_due")


if __name__ == "__main__":
    unittest.main()
