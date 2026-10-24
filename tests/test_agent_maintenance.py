"""Tests for the maintenance agents and fail-closed Nerd watchdog."""
import os
import tempfile
import time
import unittest

from bot.storage import Store
from bot.maintenance import janitor_dedupe, run_janitors
from bot.nerd import inspect_store


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
