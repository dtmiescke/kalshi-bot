"""summary.json reports where the memory file's bytes go, so a growing table
can be found without downloading the database."""
import os
import tempfile
import unittest

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
