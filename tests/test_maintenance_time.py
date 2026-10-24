"""Maintenance janitors must respect explicitly supplied timestamps."""
import tempfile
import unittest
from pathlib import Path

from bot.storage import Store
from bot.maintenance import run_janitors


class TestMaintenanceTime(unittest.TestCase):
    def test_force_run_respects_explicit_zero_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "test.sqlite"))
            try:
                result = run_janitors(store, now=0, force=True)
                self.assertTrue(result["ok"])
                self.assertEqual(result["ts"], 0)
                self.assertEqual(store.get_kv("janitors_last_run"), 0)
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
