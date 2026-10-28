"""summary.json reports where the memory file's bytes go, so a growing table
can be found without downloading the database."""
import os
import tempfile
import unittest
import unittest.mock

from bot import dashboard
from bot.storage import Store


class TestStorageReport(unittest.TestCase):
    def test_reports_biggest_tables_and_free_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(os.path.join(tmp, "t.sqlite"))
            st.db.execute("CREATE TABLE big (x TEXT)")
            st.db.executemany("INSERT INTO big VALUES (?)", [("y" * 500,)] * 4000)
            st.db.commit()
            rep = dashboard.storage_report(st)
            self.assertGreater(rep["file_mb"], 1.5)
            self.assertIn("free_mb", rep)
            tables = rep.get("tables_mb") or rep.get("table_rows")
            self.assertEqual(next(iter(tables)), "big")          # largest first
            self.assertIn("storage", dashboard.summary(st))


if __name__ == "__main__":
    unittest.main()


class TestCapsAndAuditBreakdown(unittest.TestCase):
    def test_sports_news_capped_to_newest_rows_and_audit_breakdown(self):
        import time
        from bot import storage
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(os.path.join(tmp, "t.sqlite"))
            st.db.execute("CREATE TABLE IF NOT EXISTS sports_intel_items (observed_ts INTEGER, payload_json TEXT)")
            now = int(time.time())
            st.db.executemany("INSERT INTO sports_intel_items(observed_ts, payload_json) VALUES (?, '{}')",
                              [(now - i,) for i in range(30)])
            with unittest.mock.patch.object(storage, "RAW_ROW_CAPS", (("sports_intel_items", "observed_ts", 10),)):
                storage.trim_raw(st.db, now)
            rows = [r[0] for r in st.db.execute("SELECT observed_ts FROM sports_intel_items ORDER BY observed_ts DESC")]
            self.assertEqual(rows, [now - i for i in range(10)])          # newest kept
            st.add_decision_audit({"ts": now - 5 * 86400, "ticker": "T", "decision": "rejected", "reason": "x" * 50})
            st.add_decision_audit({"ts": now, "ticker": "T", "decision": "rejected"})
            rep = dashboard.storage_report(st)
            self.assertEqual(rep["decision_audit"][0]["decision"], "rejected")
            self.assertEqual(rep["decision_audit"][0]["rows"], 2)
            self.assertEqual(rep["decision_audit"][0]["older_3d"], 1)

    def test_default_cap_leaves_room_for_readers(self):
        from bot import storage
        self.assertEqual(storage.RAW_ROW_CAPS, (("sports_intel_items", "observed_ts", 2000),))
