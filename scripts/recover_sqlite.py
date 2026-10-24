#!/usr/bin/env python3
"""Offline, non-destructive SQLite salvage. Never replaces source or bot-data."""
import argparse
import pathlib
import sqlite3
import subprocess
import sys

def check(path):
    with sqlite3.connect(path) as db:
        rows = db.execute("PRAGMA integrity_check").fetchall()
        return len(rows) == 1 and rows[0][0] == "ok", rows[:10]

def main():
    p = argparse.ArgumentParser()
    p.add_argument("source", type=pathlib.Path)
    p.add_argument("destination", type=pathlib.Path)
    a = p.parse_args()
    if not a.source.is_file() or not a.source.stat().st_size:
        p.error("source missing or empty")
    if a.destination.exists():
        p.error("destination already exists; refusing overwrite")
    if a.source.resolve() == a.destination.resolve():
        p.error("source and destination must differ")
    a.destination.parent.mkdir(parents=True, exist_ok=True)
    ok, issues = check(a.source)
    print("Source integrity:", "ok" if ok else issues, flush=True)
    if ok:
        print("Source is healthy; recovery unnecessary")
        return 0
    # sqlite3 .recover salvages damaged btrees; output is a separate candidate.
    recover = subprocess.Popen(["sqlite3", str(a.source), ".recover"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with a.destination.open("x", encoding="utf-8") as out:
        imp = subprocess.run(["sqlite3", str(a.destination)], stdin=recover.stdout,
                             stdout=out, stderr=subprocess.PIPE, text=True)
    recover.stdout.close()
    errors = recover.stderr.read()
    recover.wait()
    if recover.returncode or imp.returncode:
        print("Recovery errors:", errors[:1000], imp.stderr[:1000], file=sys.stderr)
        return 2
    healthy, issues = check(a.destination)
    print("Candidate integrity:", "ok" if healthy else issues)
    for path in (a.source, a.destination):
        with sqlite3.connect(path) as db:
            print(path, "tables:", sorted(r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))[:100])
    print("Candidate is NOT automatically published. Review data counts before restoring.")
    return 0 if healthy else 3

if __name__ == "__main__":
    sys.exit(main())
