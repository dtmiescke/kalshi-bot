"""Train without pausing the bot.

kalshi-train works on its own copy of memory and never takes the bot's lock.
It marks the database before it starts, then exports only what training added
(new rows in any table, changed kv settings, the model files) as a small
"training delta". The next bot run imports that delta into the live memory in
one transaction and saves it as usual, so trading never stops and nothing the
bot wrote in the meantime is touched.

Training only inserts rows (history windows, weights, model runs, readings)
and sets kv keys; it never updates or deletes other rows, which is what makes
"rows added after the mark" a complete record of its work.
"""
import json
import logging
import os
import shutil
import sqlite3
import time

log = logging.getLogger("bot")
META = "_delta_meta"
APPLIED_KEY = "train_delta_applied"


def _tables(db):
    return [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def _int_pk(db, table):
    """The INTEGER PRIMARY KEY column (a rowid alias), which the target assigns itself."""
    for _, name, typ, _, _, pk in db.execute(f'PRAGMA table_info("{table}")'):
        if pk == 1 and str(typ).upper() == "INTEGER":
            return name
    return None


def mark(db_path, mark_path):
    """Record where every table ends and the kv values before training starts."""
    db = sqlite3.connect(db_path)
    try:
        tables = {t: db.execute(f'SELECT COALESCE(MAX(rowid), 0) FROM "{t}"').fetchone()[0]
                  for t in _tables(db)}
        kv = dict(db.execute("SELECT k, v FROM kv").fetchall()) if "kv" in tables else {}
    finally:
        db.close()
    with open(mark_path, "w") as f:
        json.dump({"tables": tables, "kv": kv, "ts": int(time.time())}, f)


def export(db_path, mark_path, out_path, delta_id):
    """Write the rows and kv values training added since mark() to out_path."""
    with open(mark_path) as f:
        m = json.load(f)
    if os.path.exists(out_path):
        os.remove(out_path)
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(out_path)
    counts = {}
    try:
        for t in _tables(src):
            if t == "kv":
                continue
            since = m["tables"].get(t, 0)
            rows = src.execute(f'SELECT * FROM "{t}" WHERE rowid > ?', (since,)).fetchall()
            if not rows:
                continue
            ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
            out.execute(ddl)
            marks = ",".join("?" * len(rows[0]))
            out.executemany(f'INSERT INTO "{t}" VALUES({marks})', rows)
            counts[t] = len(rows)
        before = m.get("kv", {})
        changed = [(k, v) for k, v in src.execute("SELECT k, v FROM kv").fetchall() if before.get(k) != v]
        out.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
        out.executemany("INSERT INTO kv VALUES(?, ?)", changed)
        counts["kv"] = len(changed)
        out.execute(f"CREATE TABLE {META} (id TEXT, created INTEGER, counts TEXT)")
        out.execute(f"INSERT INTO {META} VALUES(?, ?, ?)", (str(delta_id), int(time.time()), json.dumps(counts)))
        out.commit()
    finally:
        src.close()
        out.close()
    return counts


def import_delta(db_path, delta_path, models_src=None, models_dst="data/models"):
    """Apply a training delta once, atomically. Returns counts, or None if already applied."""
    delta = sqlite3.connect(delta_path)
    db = sqlite3.connect(db_path)
    try:
        delta_id, counts = delta.execute(f"SELECT id, counts FROM {META}").fetchone()
        applied = db.execute("SELECT v FROM kv WHERE k=?", (APPLIED_KEY,)).fetchone()
        if applied and json.loads(applied[0]) == delta_id:
            return None
        with db:   # one transaction: all of it or none of it
            for t in _tables(delta):
                if t in (META, "kv"):
                    continue
                if t not in _tables(db):   # created on first use by training (e.g. ml_rows)
                    db.execute(delta.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                                             (t,)).fetchone()[0])
                pk = _int_pk(db, t)
                have = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
                cols = [c for c in (r[1] for r in delta.execute(f'PRAGMA table_info("{t}")'))
                        if c in have and c != pk]
                if not cols:
                    continue
                names = ",".join(f'"{c}"' for c in cols)
                rows = delta.execute(f'SELECT {names} FROM "{t}"').fetchall()
                db.executemany(f'INSERT OR IGNORE INTO "{t}" ({names}) VALUES({",".join("?" * len(cols))})', rows)
            db.executemany("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)",
                           delta.execute("SELECT k, v FROM kv").fetchall())
            db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (APPLIED_KEY, json.dumps(delta_id)))
    finally:
        delta.close()
        db.close()
    if models_src and os.path.isdir(models_src):
        os.makedirs(models_dst, exist_ok=True)
        for name in os.listdir(models_src):
            shutil.copy2(os.path.join(models_src, name), os.path.join(models_dst, name))
    return {"id": delta_id, **json.loads(counts)}
