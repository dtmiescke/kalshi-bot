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


# --- silent-failure detector (health_checks.py), run at the end of every cycle --------
HEALTH_PAUSE_KEY = "health_paused_areas"


def health_area(series):
    """Which betting area a series belongs to, as health_checks names them."""
    s = str(series or "").upper()
    if s.startswith(("KXBTC", "KXETH", "KXSOL")):
        return "crypto" if "15M" in s else "ladders"
    return "sports"


def area_paused(store, series):
    """Reason string if a critical health alarm paused this series' area, else None."""
    paused = store.get_kv(HEALTH_PAUSE_KEY) or {}
    return paused.get("all") or paused.get(health_area(series))


def cycle_stats(store, since_ts):
    """What this cycle actually produced, from the database (no extra API calls)."""
    pricing = {}
    for r in store.q("""SELECT ticker, MAX(yes_bid IS NOT NULL AND yes_ask IS NOT NULL) AS priced
                        FROM market_snapshots WHERE ts >= ? GROUP BY ticker""", (int(since_ts),)):
        area = health_area(r["ticker"])
        s = pricing.setdefault(area, {"seen": 0, "priced": 0})
        s["seen"] += 1
        s["priced"] += int(bool(r["priced"]))
    # Only decisions that passed the risk checks: each needed an outside signal,
    # so zero stored signals means signal storage broke. (Rejections for "no
    # outside signal", e.g. an exhausted odds quota, must not pause everything.)
    candidates = store.q("""SELECT COUNT(*) AS n FROM decision_audit
                            WHERE ts >= ? AND decision = 'candidate'""", (int(since_ts),))[0]["n"]
    produced = store.q("""SELECT COUNT(*) AS n FROM signal_readings
                          WHERE ts >= ? AND signal <> 'kalshi_market'""", (int(since_ts),))[0]["n"]
    return {"pricing": pricing, "signals": {"candidates": candidates, "signals_produced": produced}}


def silent_failure_check(store, since_ts, now=None, alert=None):
    """Run health_checks.run_all on this cycle's real output and fold the result
    into the Nerd's report. A critical alarm pauses only its area until a later
    cycle comes back clean; warnings are reported, never paused on."""
    from . import health_checks as hc
    now = int(time.time() if now is None else now)
    stats = cycle_stats(store, since_ts)
    open_bets = [{"ticker": b["ticker"],
                  "expected_settle_ts": store.get_kv(f"settle_after_{b['ticker']}")}
                 for b in store.open_bets()]
    alarms = hc.run_all(stats, open_bets=open_bets, now=now)
    paused = {}
    for a in alarms:
        if a.severity == "critical":
            paused.setdefault(a.area, f"health check {a.check}: {a.message}")
    previous = store.get_kv(HEALTH_PAUSE_KEY) or {}
    store.set_kv(HEALTH_PAUSE_KEY, paused)
    result = {"ts": now, "stats": stats, "paused_areas": sorted(paused),
              "alarms": [{"check": a.check, "severity": a.severity, "area": a.area,
                          "message": a.message} for a in alarms]}
    report = dict(store.get_kv("nerd_last_health") or {})
    report["silent_failures"] = result
    store.set_kv("nerd_last_health", report)
    new = sorted(set(paused) - set(previous))
    if new and alert:
        alert("Betting paused by health check",
              "; ".join(paused[a] for a in new)[:500], priority="high", tags=["warning"])
    return result
