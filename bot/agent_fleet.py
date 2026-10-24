"""Coordinated sports scanner fleet and maintenance agents.

The fleet is deliberately advisory: scanners inspect already-fetched event data,
write one health snapshot, and never create orders. The existing Engine remains
the single owner of pricing, counsel, risk checks, and execution.
"""
from __future__ import annotations

import compileall
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger(__name__)
SCANNER_COUNT = 8

# Only exact duplicate rows are removed. Distinct timestamps, quotes, decisions,
# grades, bets, lessons, and model history are preserved.
DEDUP_TABLES = {
    "market_snapshots": ("ts", "ticker", "event_ticker", "series", "title", "team",
                         "yes_bid", "yes_ask", "last_price", "volume",
                         "open_interest", "close_time", "status"),
    "odds_snapshots": ("ts", "sport", "event_id", "commence_time", "home", "away",
                       "team", "fair_prob", "n_books", "raw_json"),
    "signal_readings": ("ts", "ticker", "signal", "prob", "confidence", "detail"),
}
REQUIRED_TABLES = {"bets", "kv", "market_snapshots", "odds_snapshots",
                   "signal_readings", "decision_audit", "combo_prediction_audit"}


def _event_ticker(event):
    return str(event.get("event_ticker") or event.get("ticker") or "").strip()


def _inspect_event(event):
    """Read-only scan. Candidate means quoteable active market, not a bet signal."""
    markets = event.get("markets") or []
    active = 0
    quoteable = 0
    for market in markets:
        if str(market.get("status") or "").lower() not in ("active", "open"):
            continue
        active += 1
        bid, ask = market.get("yes_bid"), market.get("yes_ask")
        try:
            bid, ask = float(bid), float(ask)
        except (TypeError, ValueError):
            continue
        # Kalshi prices may be represented in cents or normalized fractions.
        if bid > 1:
            bid /= 100.0
        if ask > 1:
            ask /= 100.0
        if 0 <= bid <= 1 and 0 <= ask <= 1 and bid <= ask:
            quoteable += 1
    return {
        "event_ticker": _event_ticker(event),
        "series": str(event.get("series_ticker") or ""),
        "active_markets": active,
        "quoteable_markets": quoteable,
        "candidate": quoteable > 0,
    }


def scan_events(store, events_by_series, held=None, now=None):
    """Run eight bounded read-only scanner workers over unique event IDs.

    Events are deduplicated before assignment; each event is owned by exactly one
    scanner in a cycle. Held events are prioritized but not duplicated. The
    existing Engine later applies its full strategies/counsel/risk path.
    """
    now = int(now or time.time())
    held = {str(x) for x in (held or set()) if x}
    unique = {}
    for series, events in (events_by_series or {}).items():
        for event in events or []:
            ticker = _event_ticker(event)
            if not ticker:
                continue
            copy = dict(event)
            copy.setdefault("series_ticker", series)
            # Merge repeated representations without duplicating an event. Prefer
            # the representation with more market data.
            if ticker not in unique or len(copy.get("markets") or []) > len(unique[ticker].get("markets") or []):
                unique[ticker] = copy

    ordered = sorted(unique.values(), key=lambda e: (0 if _event_ticker(e) in held else 1, _event_ticker(e)))
    partitions = [[] for _ in range(SCANNER_COUNT)]
    for i, event in enumerate(ordered):
        partitions[i % SCANNER_COUNT].append(event)

    results = {}
    failures = {}
    def run_worker(worker_id, events):
        rows = []
        for event in events:
            # One bounded retry for a transient malformed event; do not create
            # orders or modify source data from a scanner worker.
            try:
                rows.append(_inspect_event(event))
            except Exception:
                try:
                    rows.append(_inspect_event(dict(event)))
                except Exception as exc:
                    raise RuntimeError(f"{_event_ticker(event)}: {exc}") from exc
        return rows

    with ThreadPoolExecutor(max_workers=SCANNER_COUNT, thread_name_prefix="sports-scan") as pool:
        future_map = {pool.submit(run_worker, i + 1, batch): i + 1
                      for i, batch in enumerate(partitions)}
        for future in as_completed(future_map):
            worker_id = future_map[future]
            try:
                results[worker_id] = future.result()
            except Exception as exc:
                failures[str(worker_id)] = str(exc)

    all_rows = [row for worker_id in sorted(results) for row in results[worker_id]]
    assigned = sum(len(x) for x in partitions)
    report = {
        "ts": now,
        "workers": SCANNER_COUNT,
        "unique_events": len(unique),
        "assigned_events": assigned,
        "duplicate_event_records_skipped": sum(len(v or []) for v in (events_by_series or {}).values()) - len(unique),
        "active_markets": sum(r["active_markets"] for r in all_rows),
        "quoteable_markets": sum(r["quoteable_markets"] for r in all_rows),
        "candidate_events": sum(1 for r in all_rows if r["candidate"]),
        "held_events_seen": sum(1 for r in all_rows if r["event_ticker"] in held),
        "worker_failures": failures,
        "status": "degraded" if failures else "ok",
    }
    store.set_kv("agent_fleet_live_scan", report)
    return report


def _quick_check(store):
    with store.lock:
        rows = store.db.execute("PRAGMA quick_check").fetchall()
        result = [str(row[0]) for row in rows]
        tables = {row[0] for row in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = sorted(REQUIRED_TABLES - tables)
    return result, missing


def code_nerd_preflight(store, root="."):
    """Check database integrity, required schema, and Python syntax before work."""
    check, missing = _quick_check(store)
    source_root = Path(root) / "bot"
    required_files = ("main.py", "engine.py", "config.py", "storage.py", "guard.py",
                      "risk.py", "kalshi_client.py", "agent_fleet.py")
    missing_files = [name for name in required_files if not (source_root / name).is_file()]
    compile_ok = compileall.compile_dir(str(source_root), quiet=1, force=False)
    problems = []
    if check != ["ok"]:
        problems.append("SQLite quick_check failed: " + "; ".join(check[:5]))
    if missing:
        problems.append("missing required tables: " + ", ".join(missing))
    if missing_files:
        problems.append("missing required source files: " + ", ".join(missing_files))
    if not compile_ok:
        problems.append("Python compile check failed under bot/")
    report = {"ts": int(time.time()), "status": "blocked" if problems else "ok",
              "sqlite": check, "missing_tables": missing,
              "python_compile_ok": bool(compile_ok), "problems": problems}
    try:
        store.set_kv("code_nerd_health", report)
        if problems:
            store.set_kv("fleet_safety_hold", "; ".join(problems))
        elif store.get_kv("fleet_safety_hold"):
            store.set_kv("fleet_safety_hold", None)
    except sqlite3.DatabaseError:
        # If the database itself is corrupt, do not try to repair it in place.
        log.exception("Code Nerd could not persist health state; failing closed")
    if problems:
        raise RuntimeError("Code Nerd preflight blocked scheduled work: " + "; ".join(problems))
    return report


def code_nerd_postflight(store):
    """Verify persisted data after work; never rewrite source code automatically."""
    check, missing = _quick_check(store)
    problems = []
    if check != ["ok"]:
        problems.append("SQLite quick_check failed after cycle: " + "; ".join(check[:5]))
    if missing:
        problems.append("missing required tables: " + ", ".join(missing))
    report = {"ts": int(time.time()), "status": "degraded" if problems else "ok",
              "sqlite": check, "missing_tables": missing, "problems": problems}
    try:
        store.set_kv("code_nerd_health", report)
        if problems:
            store.set_kv("fleet_safety_hold", "; ".join(problems))
    except sqlite3.DatabaseError:
        log.exception("Code Nerd could not persist postflight health state")
    if problems:
        log.critical("Code Nerd detected a post-cycle integrity problem: %s", problems)
    return report


def janitor_deduplicate(store):
    """Remove exact duplicate raw observation rows only, preserving distinct history."""
    counts = {}
    with store.lock:
        for table, columns in DEDUP_TABLES.items():
            before = store.db.total_changes
            cols = ", ".join('"' + col + '"' for col in columns)
            store.db.execute(
                f'DELETE FROM "{table}" WHERE rowid NOT IN '
                f'(SELECT MIN(rowid) FROM "{table}" GROUP BY {cols})')
            counts[table] = store.db.total_changes - before
        store.db.commit()
    report = {"ts": int(time.time()), "deleted_exact_duplicates": counts,
              "total_deleted": sum(counts.values()), "status": "ok"}
    store.set_kv("janitor_dedupe_health", report)
    return report


def janitor_retention(store, now=None):
    """Use existing safe retention policy; learning, bets and grades are never pruned."""
    now = int(now or time.time())
    last = float(store.get_kv("janitor_retention_last_ts", 0) or 0)
    if now - last < 24 * 3600:
        return {"ts": now, "status": "skipped", "reason": "daily retention not due"}
    store.compact()
    store.set_kv("janitor_retention_last_ts", now)
    report = {"ts": now, "status": "ok",
              "policy": {"market_snapshots_days": 14, "flow_trades_days": 7,
                         "signal_readings_days": 30, "odds_snapshots_days": 30,
                         "ml_rows_days": 60,
                         "preserved": ["bets", "grades", "lessons", "weights", "model history"]}}
    store.set_kv("janitor_retention_health", report)
    return report


def run_janitors(store, now=None):
    """Isolate janitor failures so diagnostics remain available."""
    results = {}
    for name, fn in (("dedupe", janitor_deduplicate), ("retention", janitor_retention)):
        try:
            results[name] = fn(store) if name == "dedupe" else fn(store, now=now)
        except Exception as exc:
            results[name] = {"status": "error", "error": str(exc), "ts": int(now or time.time())}
            log.exception("Janitor %s failed", name)
    store.set_kv("janitors_health", {"ts": int(now or time.time()), "agents": results})
    return results
