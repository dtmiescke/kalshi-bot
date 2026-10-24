"""Two conservative data janitors for the existing SQLite store.

Janitor A removes duplicate raw snapshots and adds uniqueness constraints.
Janitor B removes invalid raw rows and applies existing retention windows.
Neither janitor deletes bets, forecasts, grades, lessons, model weights, or audit history.
"""
import time


RAW_KEYS = {
    "market_snapshots": ("ts", "ticker"),
    "odds_snapshots": ("ts", "event_id", "team"),
    "signal_readings": ("ts", "ticker", "signal"),
}


def janitor_dedupe(store):
    """Remove duplicate raw observations by natural key, preserving oldest rowid."""
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
                cur = db.execute(
                    f"DELETE FROM {table} WHERE rowid NOT IN "
                    f"(SELECT MIN(rowid) FROM {table} GROUP BY {group})")
                duplicates = max(0, cur.rowcount)
                index = "ux_" + table + "_natural_key"
                db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} ({group})")
                removed[table] = {"invalid": invalid, "duplicates": duplicates}
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"janitor": "dedupe", "removed": removed, "ts": int(time.time())}


def janitor_retention(store):
    """Trim only raw time-series data under existing retention policy."""
    # Existing Store.compact retains learned weights, grades, bets and lessons.
    before = {}
    for table in ("market_snapshots", "flow_trades", "signal_readings", "odds_snapshots"):
        try:
            before[table] = store.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        except Exception:
            before[table] = None
    store.compact()
    after = {}
    for table in before:
        try:
            after[table] = store.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        except Exception:
            after[table] = None
    return {"janitor": "retention", "before": before, "after": after,
            "ts": int(time.time())}


def run_janitors(store, now=None, force=False, every_hours=6):
    """Run both janitors at a bounded cadence; never hide cleanup exceptions."""
    now = float(now or time.time())
    last = float(store.get_kv("janitors_last_run", 0) or 0)
    if not force and now - last < max(1, float(every_hours)) * 3600:
        return {"ok": True, "skipped": "not_due", "ts": int(now)}
    # Dedupe first so the unique constraints protect subsequent writes.
    a = janitor_dedupe(store)
    b = janitor_retention(store)
    result = {"ok": True, "janitor_a": a, "janitor_b": b, "ts": int(now)}
    store.set_kv("janitors_last_result", result)
    store.set_kv("janitors_last_run", now)
    return result
