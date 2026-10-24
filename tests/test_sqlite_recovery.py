import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recover_sqlite.py"


class SQLiteRecoverySafetyTests(unittest.TestCase):
    def test_healthy_source_is_not_modified_or_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite"
            destination = Path(tmp) / "candidate.sqlite"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
                db.execute("INSERT INTO kv VALUES ('learning', 'preserve-me')")
            before = source.read_bytes()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(source), str(destination)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(destination.exists())
            self.assertIn("recovery unnecessary", result.stdout)

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite"
            destination = Path(tmp) / "candidate.sqlite"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE t (v TEXT)")
                db.execute("INSERT INTO t VALUES ('source')")
            destination.write_bytes(b"keep-this-file")
            before = destination.read_bytes()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(source), str(destination)],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
