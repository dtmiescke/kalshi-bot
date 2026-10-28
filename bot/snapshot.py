"""Keep the memory file small enough to upload reliably.

The saved copy of the database does not need its indexes: they are rebuilt from the data and only
make lookups fast. Indexes were about half of the compressed file (54.6 MB compressed, 40.0 MB
without them, measured on the real database). So the copy that is uploaded has its explicit indexes
dropped (their definitions are kept in a small table inside it), and the next run recreates them
right after it loads the file. The live database is never touched.

    python -m bot.snapshot strip PATH     # on the copy about to be compressed
    python -m bot.snapshot restore PATH   # on the file just decompressed
"""
import sqlite3
import sys

TABLE = "_stripped_indexes"


def strip_indexes(path):
    """Drop every explicit index from the file at `path`, remembering its definition. Returns how many."""
    db = sqlite3.connect(path)
    try:
        idx = db.execute("""SELECT name, sql FROM sqlite_master
                            WHERE type='index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'""").fetchall()
        db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE}(name TEXT PRIMARY KEY, sql TEXT)")
        for name, sql in idx:
            db.execute(f"INSERT OR REPLACE INTO {TABLE} VALUES(?, ?)", (name, sql))
            db.execute(f'DROP INDEX IF EXISTS "{name}"')
        db.commit()
        db.execute("VACUUM")
        return len(idx)
    finally:
        db.close()


def restore_indexes(path):
    """Recreate the indexes strip_indexes() removed. A file without them (an older snapshot) is left alone."""
    db = sqlite3.connect(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return 0
        made = 0
        for name, sql in db.execute(f"SELECT name, sql FROM {TABLE} ORDER BY name").fetchall():
            try:
                db.execute(sql)
                made += 1
            except sqlite3.OperationalError as exc:
                if "already exists" not in str(exc):
                    raise
        db.execute(f"DROP TABLE {TABLE}")
        db.commit()
        return made
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("strip", "restore"):
        sys.exit("usage: python -m bot.snapshot strip|restore PATH")
    n = (strip_indexes if sys.argv[1] == "strip" else restore_indexes)(sys.argv[2])
    print(f"{sys.argv[1]}: {n} indexes")
