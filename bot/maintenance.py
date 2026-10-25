"""Two conservative data janitors for the existing SQLite store.

Janitor A removes duplicate raw snapshots and adds uniqueness constraints.
Janitor B removes invalid raw rows and applies existing retention windows.
Neither janitor deletes bets, forecasts, grades, lessons, model weights, or audit history.
"""
import sqlite3
import time

from .storage import RAW_RETENTION, trim_raw


RAW_KEYS = {
    "market_snapshots": ("ts", "ticker"),
    "odds_snapshots": ("ts", "event_id", "team"),
    "signal_readings": ("ts", "ticker", "signal"),
}


def janitor_dedupe(store, now=None):
    """Remove duplicate raw observations by natural key, preserving oldest rowid."""
    now = int(time.time() if now is None else now)
    removed = {}
    with store.lock:
        db = store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            for table, keys in RAW_KEYS.items():
                where = " AND ".join(f"{key} IS NOT NULL AND CAST({key} AS TEXT) <> ''"
                                     for key in keys)
                # Junk records without a natural key cannot be safely joined or graded.
                cur = db.execute(f"DELETE FROM {table} WHERE NOT ({where})")
                invalid = max(0, cur.rowcount)
                group = ", ".join(keys)
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
                completeness = " + ".join(f"({column} IS NOT NULL)" for column in columns)
                # Keep the richest record for a natural key; on ties preserve the
                # earliest row so cleanup never prefers a later partial response.
                cur = db.execute(
                    f"DELETE FROM {table} WHERE rowid IN ("
                    f"SELECT rowid FROM (SELECT rowid, ROW_NUMBER() OVER "
                    f"(PARTITION BY {group} ORDER BY ({completeness}) DESC, rowid ASC) AS rn "
                    f"FROM {table}) WHERE rn > 1)")
                duplicates = max(0, cur.rowcount)
                index = "ux_" + table + "_natural_key"
                db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} ({group})")
                removed[table] = {"invalid": invalid, "duplicates": duplicates}
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"janitor": "dedupe", "removed": removed, "ts": now}


def janitor_retention(store, now=None):
    """Trim only raw time-series data under existing retention policy."""
    # Same retention windows as Store.compact (storage.RAW_RETENTION), but no
    # VACUUM every six hours; the daily compact owns physical compaction.
    now = int(time.time() if now is None else now)
    before, after = {}, {}
    with store.lock:
        store.db.execute("BEGIN IMMEDIATE")
        try:
            for table in RAW_RETENTION:
                try:
                    before[table] = store.db.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.OperationalError:
                    continue            # optional table not created yet
            trim_raw(store.db, now)
            for table in before:
                after[table] = store.db.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            store.db.commit()
        except Exception:
            store.db.rollback()
            raise
    return {"janitor": "retention", "before": before, "after": after,
            "ts": now}


# Deleting rows frees pages but doesn't shrink the file until VACUUM. After a
# big trim (e.g. a tighter retention window taking effect) compact at once
# instead of waiting up to a day for the scheduled compaction.
COMPACT_AFTER_ROWS = 100_000


def run_janitors(store, now=None, force=False, every_hours=6):
    """Run both janitors at a bounded cadence; never hide cleanup exceptions."""
    now = float(time.time() if now is None else now)
    last = float(store.get_kv("janitors_last_run", 0) or 0)
    if not force and now - last < max(1, float(every_hours)) * 3600:
        return {"ok": True, "skipped": "not_due", "ts": int(now)}
    # Dedupe first so the unique constraints protect subsequent writes.
    a = janitor_dedupe(store, now=now)
    b = janitor_retention(store, now=now)
    result = {"ok": True, "janitor_a": a, "janitor_b": b, "ts": int(now)}
    trimmed = sum(b["before"][t] - b["after"].get(t, b["before"][t]) for t in b["before"])
    if trimmed >= COMPACT_AFTER_ROWS:
        store.compact()
        store.set_kv("last_compact", now)
        result["compacted_after_rows"] = trimmed
    store.set_kv("janitors_last_result", result)
    store.set_kv("janitors_last_run", now)
    return result
