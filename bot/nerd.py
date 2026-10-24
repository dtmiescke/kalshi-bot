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

# Critical columns whose absence would make order reconciliation, settlement, or
# honest performance reporting unreliable even if the table itself still exists.
REQUIRED_COLUMNS = {
    "bets": {"id", "mode", "ticker", "order_id", "contracts", "stake", "fee",
             "status", "pnl", "graded_ts"},
    "decision_audit": {"ticker", "decision", "outcome", "brier"},
    "combo_prediction_audit": {"ticker", "leg_count", "outcome", "brier"},
    "kv": {"k", "v"},
}


REQUIRED_SOURCE_PATHS = (
    "bot/main.py", "bot/engine.py", "bot/config.py", "bot/storage.py",
    "bot/guard.py", "bot/risk.py", "bot/kalshi_client.py", "bot/council.py",
    "bot/sports_fleet.py", "bot/sports_counsel.py", "bot/maintenance.py",
    "bot/nerd.py", "bot/live/runner.py", ".github/workflows/bot.yml",
    ".github/load.sh", ".github/save.sh",
)


def inspect_code(root=None):
    """Verify critical project structure before market work starts.

    This is detection/containment, not source-code self-modification. Missing
    required files raise a clear incident so the scheduled workflow fails closed.
    """
    from pathlib import Path
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    missing = [relative for relative in REQUIRED_SOURCE_PATHS
               if not (root / relative).is_file()]
    report = {"ts": int(time.time()), "ok": not missing,
              "required_paths": len(REQUIRED_SOURCE_PATHS), "missing_paths": missing}
    if missing:
        report["error"] = "required source structure missing: " + ", ".join(missing)
        raise RuntimeError(report["error"])
    return report


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
            missing_columns = {}
            for table, required in REQUIRED_COLUMNS.items():
                present = {row[1] for row in db.execute(
                    f"PRAGMA table_info({table})").fetchall()}
                absent = sorted(required - present)
                if absent:
                    missing_columns[table] = absent
            report["checks"]["missing_columns"] = missing_columns
            if missing_columns:
                detail = "; ".join(
                    f"{table}: {', '.join(columns)}"
                    for table, columns in sorted(missing_columns.items()))
                raise RuntimeError("required SQLite columns missing: " + detail)
            pragma = "integrity_check" if full_integrity else "quick_check"
            rows = [row[0] for row in db.execute(f"PRAGMA {pragma}").fetchall()]
            report["checks"][pragma] = rows[:20]
            if rows != ["ok"]:
                raise RuntimeError(f"SQLite {pragma} failed: {rows[:5]}")
            db_path = db.execute("PRAGMA database_list").fetchone()[2]
            report["checks"]["db_path_exists"] = bool(db_path and os.path.exists(db_path))
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
