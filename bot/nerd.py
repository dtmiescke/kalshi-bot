"""Nerd watchdog: structural/runtime checks and safe fail-closed behavior.

This monitor can validate runtime state and report failures. It deliberately does
not rewrite or deploy its own source code. Code fixes must go through tested PRs.
"""
import os
import sqlite3
import time


REQUIRED_TABLES = {
    "market_snapshots", "odds_snapshots", "flow_trades", "signal_readings",
    "bets", "weight_history", "signal_grades", "graded_markets", "lessons", "kv",
    "decision_audit", "combo_prediction_audit",
}


def inspect_store(store, full_integrity=False):
    """Check required schema and SQLite health; raise on unsafe state."""
    report = {"ts": int(time.time()), "ok": False, "checks": {}}
    try:
        with store.lock:
            db = store.db
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            missing = sorted(REQUIRED_TABLES - tables)
            report["checks"]["missing_tables"] = missing
            if missing:
                raise RuntimeError("required SQLite tables missing: " + ", ".join(missing))
            pragma = "integrity_check" if full_integrity else "quick_check"
            rows = [row[0] for row in db.execute(f"PRAGMA {pragma}").fetchall()]
            report["checks"][pragma] = rows[:20]
            if rows != ["ok"]:
                raise RuntimeError(f"SQLite {pragma} failed: {rows[:5]}")
            report["checks"]["db_path_exists"] = os.path.exists(getattr(db, "database", ""))
        report["ok"] = True
        return report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            store.set_kv("nerd_last_incident", report)
        except Exception:
            pass
        raise RuntimeError("Nerd watchdog blocked the cycle; inspect nerd_last_incident. "
                           + report["error"]) from exc


def record_success(store, report):
    store.set_kv("nerd_last_health", report)
    store.set_kv("nerd_last_healthy_ts", report.get("ts", int(time.time())))
