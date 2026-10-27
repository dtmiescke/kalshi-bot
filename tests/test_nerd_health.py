"""Nerd health aggregation must retain code-structure checks."""
import unittest

from bot.main import _attach_code_health


class TestNerdHealthAggregation(unittest.TestCase):
    def test_code_health_survives_database_recheck(self):
        health = {"ok": True, "checks": {"quick_check": ["ok"]}}
        code = {"ok": True, "required_paths": 16, "missing_paths": []}
        result = _attach_code_health(health, code)
        self.assertIs(result, health)
        self.assertEqual(result["checks"]["source_structure"], code)
        self.assertEqual(result["checks"]["quick_check"], ["ok"])

    def test_code_health_is_added_when_check_map_is_absent(self):
        result = _attach_code_health({"ok": True}, {"ok": False, "missing_paths": ["bot/x.py"]})
        self.assertEqual(result["checks"]["source_structure"]["ok"], False)



    def test_watchdog_detects_missing_council_ledger(self):
        import os
        import tempfile
        from bot.nerd import inspect_store
        from bot.storage import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "health.db"))
            try:
                store.db.execute("DROP TABLE council_votes")
                store.db.commit()
                with self.assertRaisesRegex(RuntimeError, "required SQLite tables missing"):
                    inspect_store(store)
                incident = store.get_kv("nerd_last_incident", {})
                self.assertIn("council_votes", incident["checks"]["missing_tables"])
            finally:
                store.db.close()

    def test_watchdog_detects_missing_council_grading_column(self):
        import os
        import tempfile
        from bot.nerd import inspect_store
        from bot.storage import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "health.db"))
            try:
                store.db.execute("ALTER TABLE council_votes DROP COLUMN correct")
                store.db.commit()
                with self.assertRaisesRegex(RuntimeError, "required SQLite columns missing"):
                    inspect_store(store)
                incident = store.get_kv("nerd_last_incident", {})
                self.assertIn("correct", incident["checks"]["missing_columns"]["council_votes"])
            finally:
                store.db.close()

    def test_watchdog_detects_missing_critical_ledger_column(self):
        import os
        import tempfile
        from bot.nerd import inspect_store
        from bot.storage import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "health.db"))
            try:
                store.db.execute("ALTER TABLE bets DROP COLUMN order_id")
                store.db.commit()
                with self.assertRaisesRegex(RuntimeError, "required SQLite columns missing"):
                    inspect_store(store)
                incident = store.get_kv("nerd_last_incident", {})
                self.assertIn("missing_columns", incident["checks"])
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
