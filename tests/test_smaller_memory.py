"""The saved memory file leaves out its indexes and gets them back on load; a long stream trims as it goes."""
import gzip
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from bot import snapshot
from bot.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def make_db(path, rows=2000):
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE t(id INTEGER PRIMARY KEY, a TEXT, b INTEGER, UNIQUE(a, b));
                        CREATE INDEX ix_a ON t(a);
                        CREATE UNIQUE INDEX ux_b ON t(b, a);""")
    db.executemany("INSERT INTO t(a, b) VALUES(?, ?)", [(f"row-{i}", i) for i in range(rows)])
    db.commit()
    db.close()


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = os.path.join(self.d.name, "m.sqlite")
        make_db(self.p)

    def tearDown(self):
        self.d.cleanup()

    def names(self):
        db = sqlite3.connect(self.p)
        try:
            return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            db.close()

    def test_strip_then_restore_gives_back_the_same_indexes_and_data(self):
        before = self.names()
        self.assertIn("ix_a", before)
        self.assertEqual(snapshot.strip_indexes(self.p), 2)
        stripped = self.names()
        self.assertNotIn("ix_a", stripped)
        self.assertNotIn("ux_b", stripped)
        self.assertTrue(any(n.startswith("sqlite_autoindex") for n in stripped))     # the table's own UNIQUE stays
        self.assertEqual(snapshot.restore_indexes(self.p), 2)
        self.assertEqual(self.names(), before)
        db = sqlite3.connect(self.p)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2000)
        self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (snapshot.TABLE,)).fetchone())
        plan = " ".join(str(r) for r in db.execute("EXPLAIN QUERY PLAN SELECT * FROM t WHERE b=5"))
        self.assertIn("ux_b", plan)                                                    # lookups use the index again
        db.close()

    def test_the_stripped_file_is_smaller_compressed(self):
        raw = Path(self.p).read_bytes()
        snapshot.strip_indexes(self.p)
        self.assertLess(len(gzip.compress(Path(self.p).read_bytes())), len(gzip.compress(raw)))

    def test_restoring_an_older_file_without_the_helper_table_does_nothing(self):
        self.assertEqual(snapshot.restore_indexes(self.p), 0)
        self.assertIn("ix_a", self.names())

    def test_a_restore_is_safe_to_repeat_and_the_command_line_works(self):
        snapshot.strip_indexes(self.p)
        for verb in ("restore", "restore"):
            r = subprocess.run([sys.executable, "-m", "bot.snapshot", verb, self.p], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ix_a", self.names())

    def test_the_real_schema_survives_a_round_trip(self):
        path = os.path.join(self.d.name, "real.sqlite")
        Store(path).db.close()
        db = sqlite3.connect(path)
        want = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        db.close()
        self.assertIn("ix_snap", want)
        snapshot.strip_indexes(path)
        snapshot.restore_indexes(path)
        db = sqlite3.connect(path)
        got = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        db.close()
        self.assertEqual(got, want)


class TestScripts(unittest.TestCase):
    def test_save_strips_before_compressing_and_load_restores_before_the_check(self):
        save = (ROOT / ".github" / "save.sh").read_text(encoding="utf-8")
        load = (ROOT / ".github" / "load.sh").read_text(encoding="utf-8")
        self.assertLess(save.index("bot.snapshot strip"), save.index('gzip -n -c "$WORK/bot.snapshot.sqlite"'))
        self.assertLess(load.index("bot.snapshot restore"), load.index("PRAGMA quick_check"))


class TestInStreamTrim(unittest.TestCase):
    def test_old_raw_rows_go_and_it_runs_about_hourly(self):
        from bot.main import trim_in_stream
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            now = time.time()
            for i in range(20):                                    # flow trades are kept 2 days; these are 5 days old
                st.x("INSERT INTO flow_trades(trade_id, ticker, ts, count, yes_price, taker_side) VALUES(?,?,?,?,?,?)",
                     (f"old{i}", "KXNFLGAME-X", int(now - 5 * 86400), 1, 0.5, "yes"))
            st.x("INSERT INTO flow_trades(trade_id, ticker, ts, count, yes_price, taker_side) VALUES(?,?,?,?,?,?)",
                 ("new", "KXNFLGAME-X", int(now - 60), 1, 0.5, "yes"))
            first = trim_in_stream(st, now=now)
            self.assertIsNotNone(first)
            self.assertIn("retention", first)
            self.assertEqual(st.q("SELECT COUNT(*) c FROM flow_trades")[0]["c"], 1)     # only the recent one is left
            self.assertIsNone(trim_in_stream(st, now=now + 600))    # not due again for an hour
            self.assertIsNotNone(trim_in_stream(st, now=now + 3700))


if __name__ == "__main__":
    unittest.main()
