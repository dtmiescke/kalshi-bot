"""Eight coordinated, read-only workers for the existing sports event queue.

The fleet does not replace the Engine. It scans the already-fetched event set,
deduplicates event IDs, records market-data health, and hands candidates back to
the existing strategy/counsel/risk path. It never places orders or writes trade rows.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)
SCANNER_COUNT = 8


def _ticker(event):
    return str(event.get("event_ticker") or event.get("ticker") or "").strip()


def _inspect(event):
    active = quoteable = 0
    for market in event.get("markets") or []:
        if str(market.get("status") or "").lower() not in ("active", "open"):
            continue
        active += 1
        try:
            bid, ask = float(market.get("yes_bid")), float(market.get("yes_ask"))
        except (TypeError, ValueError):
            continue
        # Kalshi APIs may represent prices in cents or normalized fractions.
        if bid > 1:
            bid /= 100.0
        if ask > 1:
            ask /= 100.0
        if 0 <= bid <= ask <= 1:
            quoteable += 1
    return {"event_ticker": _ticker(event),
            "series": str(event.get("series_ticker") or ""),
            "active_markets": active, "quoteable_markets": quoteable,
            "candidate": quoteable > 0}


def scan_events(store, events_by_series, held=None, now=None):
    """Scan every unique event once across eight workers; always prioritize held events.

    Workers only read market data. They produce telemetry/candidate hints; the
    existing Engine performs probability analysis, counsel, risk checks and execution.
    """
    now = int(now or time.time())
    held = {str(x) for x in (held or set()) if x}
    unique = {}
    raw_count = 0
    for series, events in (events_by_series or {}).items():
        for event in events or []:
            raw_count += 1
            ticker = _ticker(event)
            if not ticker:
                continue
            item = dict(event)
            item.setdefault("series_ticker", series)
            current = unique.get(ticker)
            if current is None or len(item.get("markets") or []) > len(current.get("markets") or []):
                unique[ticker] = item

    ordered = sorted(unique.values(), key=lambda e: (
        0 if _ticker(e) in held else 1, _ticker(e)))
    partitions = [[] for _ in range(SCANNER_COUNT)]
    for index, event in enumerate(ordered):
        partitions[index % SCANNER_COUNT].append(event)

    def worker(events):
        output = []
        for event in events:
            try:
                output.append(_inspect(event))
            except Exception as exc:
                # One safe retry for malformed/transient event payloads.
                try:
                    output.append(_inspect(dict(event)))
                except Exception as retry_exc:
                    raise RuntimeError(f"{_ticker(event)}: {retry_exc}") from exc
        return output

    results, failures = {}, {}
    with ThreadPoolExecutor(max_workers=SCANNER_COUNT,
                            thread_name_prefix="sports-scanner") as pool:
        futures = {pool.submit(worker, batch): i for i, batch in enumerate(partitions, 1)}
        for future in as_completed(futures):
            worker_id = futures[future]
            try:
                results[worker_id] = future.result()
            except Exception as exc:
                failures[str(worker_id)] = str(exc)

    rows = [row for i in sorted(results) for row in results[i]]
    worker_stats = {}
    for worker_id in range(1, SCANNER_COUNT + 1):
        own = results.get(worker_id, [])
        worker_stats[str(worker_id)] = {
            "events_assigned": len(partitions[worker_id - 1]),
            "events_scanned": len(own),
            "active_markets": sum(x["active_markets"] for x in own),
            "quoteable_markets": sum(x["quoteable_markets"] for x in own),
            "candidate_events": sum(bool(x["candidate"]) for x in own),
            "status": "error" if str(worker_id) in failures else "ok",
        }
    report = {
        "ts": now, "status": "degraded" if failures else "ok",
        "workers": SCANNER_COUNT, "unique_events": len(unique),
        "raw_event_records": raw_count,
        "duplicate_event_records_skipped": raw_count - len(unique),
        "events_scanned": len(rows),
        "active_markets": sum(x["active_markets"] for x in rows),
        "quoteable_markets": sum(x["quoteable_markets"] for x in rows),
        "candidate_events": sum(bool(x["candidate"]) for x in rows),
        "held_events_seen": sum(_ticker(x) in held for x in ordered),
        "worker_failures": failures, "worker_stats": worker_stats,
        "authority": "read_only_hints_existing_engine_decides",
    }
    store.set_kv("sports_scanner_fleet_health", report)
    return report
