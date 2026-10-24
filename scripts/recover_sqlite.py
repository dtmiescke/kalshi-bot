#!/usr/bin/env python3
"""Offline, non-destructive SQLite salvage. Never replaces source or bot-data."""
import argparse
import pathlib
import sqlite3
import subprocess
import sys
import tempfile


def check(path):
    with sqlite3.connect(path) as db:
        rows = db.execute("PRAGMA integrity_check").fetchall()
        return len(rows) == 1 and rows[0][0] == "ok", rows[:10]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("destination", type=pathlib.Path)
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    if not source.is_file() or not source.stat().st_size:
        parser.error("source missing or empty")
    if destination.exists():
        parser.error("destination already exists; refusing overwrite")
    if source == destination:
        parser.error("source and destination must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)

    healthy, issues = check(source)
    print("Source integrity:", "ok" if healthy else issues, flush=True)
    if healthy:
        print("Source is healthy; recovery unnecessary. No candidate created.")
        return 0

    # Keep generated SQL separate from the destination database. Never direct
    # sqlite3 stdout into the destination path (that would truncate/corrupt it).
    with tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", suffix=".recover.sql", delete=False
    ) as sql_file:
        sql_path = pathlib.Path(sql_file.name)
    try:
        recover = subprocess.run(
            ["sqlite3", str(source), ".recover"],
            stdout=sql_path.open("w", encoding="utf-8"),
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if recover.returncode != 0:
            print("SQLite .recover failed:", recover.stderr[:3000], file=sys.stderr)
            return 2
        if sql_path.stat().st_size == 0:
            print("SQLite .recover produced no SQL; refusing empty candidate.", file=sys.stderr)
            return 2

        # .bail on makes import stop at the first SQL error. The destination
        # is created by SQLite itself and remains separate from the source.
        importer = subprocess.run(
            ["sqlite3", str(destination), ".bail on", f".read {sql_path}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if importer.returncode != 0:
            print("Recovery SQL import failed:", importer.stderr[:3000], file=sys.stderr)
            return 2
    finally:
        sql_path.unlink(missing_ok=True)

    healthy, issues = check(destination)
    print("Candidate integrity:", "ok" if healthy else issues)
    with sqlite3.connect(destination) as db:
        tables = sorted(
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        print("Candidate tables:", tables)
        for table in ("bets", "market_snapshots", "signal_readings", "kv", "decision_audit"):
            if table in tables:
                try:
                    count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    print(f"Rows {table}: {count}")
                except sqlite3.DatabaseError as exc:
                    print(f"Could not count {table}: {exc}")
    print("Candidate is NOT automatically published. Review table counts before restoring.")
    return 0 if healthy else 3


if __name__ == "__main__":
    sys.exit(main())
