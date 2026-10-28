"""Two conservative data janitors for the existing SQLite store.

Janitor A removes duplicate raw snapshots and adds uniqueness constraints.
Janitor B removes invalid raw rows and applies existing retention windows.
Neither janitor deletes bets, forecasts, grades, lessons, model weights, or audit history.
"""
import json
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
    # VACUUM every pass; the daily compact (or a big trim) owns physical compaction.
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


# decision_audit keeps every decision forever (calibration and backtests read
# model_prob/outcome), but its signals_json snapshot with per-signal details is
# read only for the newest graded rows (about a day of them). Older rows keep
# each signal's probability and confidence plus small fields; bulky nested
# details are dropped. It was 270 MB of an 888 MB database.
AUDIT_DETAIL_DAYS = 3
AUDIT_SLIM_OVER = 1500      # bytes; smaller snapshots are left alone
AUDIT_SLIM_BATCH = 50_000


def slim_signals(raw):
    """Compact one signals_json snapshot; returns the new text."""
    try:
        sig = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return "{}"
    if not isinstance(sig, dict):
        return "{}"
    out = {}
    for name, v in sig.items():
        if isinstance(v, dict) and any(k in v for k in ("p", "prob")):
            p, c = v.get("p", v.get("prob")), v.get("c", v.get("confidence"))
            out[name] = {"p": round(p, 4) if isinstance(p, (int, float)) else p,
                         "c": round(c, 3) if isinstance(c, (int, float)) else c}
        else:
            text = json.dumps(v, default=str)
            if len(text) <= 200:          # small tags (regime, setups) stay
                out[name] = v
    return json.dumps(out, separators=(",", ":"), default=str)


def janitor_slim_audit(store, now=None, keep_days=AUDIT_DETAIL_DAYS, batch=AUDIT_SLIM_BATCH):
    now = int(time.time() if now is None else now)
    cutoff = now - int(keep_days * 86400)
    saved = slimmed = 0
    with store.lock:
        db = store.db
        try:
            rows = db.execute("""SELECT id, signals_json FROM decision_audit
                                 WHERE ts < ? AND length(signals_json) > ? LIMIT ?""",
                              (cutoff, AUDIT_SLIM_OVER, batch)).fetchall()
        except sqlite3.OperationalError:
            return {"janitor": "slim_audit", "slimmed": 0, "bytes_saved": 0, "ts": now}
        db.execute("BEGIN IMMEDIATE")
        try:
            for rid, raw in rows:
                new = slim_signals(raw)
                if len(new) < len(raw or ""):
                    db.execute("UPDATE decision_audit SET signals_json=? WHERE id=?", (new, rid))
                    saved += len(raw) - len(new)
                    slimmed += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"janitor": "slim_audit", "slimmed": slimmed, "bytes_saved": saved, "ts": now}


# Deleting rows frees pages but doesn't shrink the file until VACUUM. After a
# big trim (e.g. a tighter retention window taking effect) compact at once
# instead of waiting up to a day for the scheduled compaction.
COMPACT_AFTER_ROWS = 100_000
COMPACT_AFTER_BYTES = 50_000_000


def run_janitors(store, now=None, force=False, every_hours=1):
    """Run both janitors at a bounded cadence; never hide cleanup exceptions.

    Hourly: cycles now run back to back (#44), so raw data piles up ~12 MB/hour
    and a 6-hour gap let the memory file approach GitHub's size limit."""
    now = float(time.time() if now is None else now)
    last = float(store.get_kv("janitors_last_run", 0) or 0)
    if not force and now - last < max(1, float(every_hours)) * 3600:
        return {"ok": True, "skipped": "not_due", "ts": int(now)}
    # Dedupe first so the unique constraints protect subsequent writes.
    a = janitor_dedupe(store, now=now)
    b = janitor_retention(store, now=now)
    c = janitor_slim_audit(store, now=now)
    result = {"ok": True, "janitor_a": a, "janitor_b": b, "janitor_c": c, "ts": int(now)}
    trimmed = sum(b["before"][t] - b["after"].get(t, b["before"][t]) for t in b["before"])
    if trimmed >= COMPACT_AFTER_ROWS or c["bytes_saved"] >= COMPACT_AFTER_BYTES:
        store.compact()
        store.set_kv("last_compact", now)
        result["compacted_after_rows"] = trimmed
    store.set_kv("janitors_last_result", result)
    store.set_kv("janitors_last_run", now)
    return result
