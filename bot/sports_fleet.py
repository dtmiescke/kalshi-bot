"""Eight bounded sports market scanners sharing one canonical event stream.

Scanners are read-only observers. They never place orders or duplicate market writes;
the existing Engine remains the single strategy/risk/execution authority.
"""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor


SCANNER_ROLES = (
    "schedule_and_status", "market_coverage", "quote_quality", "liquidity_watch",
    "price_dislocation_watch", "matchup_evidence_watch", "live_state_watch",
    "independent_sanity_check",
)


def _ticker(event):
    return str(event.get("event_ticker") or event.get("ticker") or "").strip()


def _merge_event(target, incoming):
    """Merge duplicate event records while deduplicating markets by ticker."""
    merged = dict(target)
    for key, value in incoming.items():
        if merged.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            merged[key] = value
    markets = {}
    for market in (target.get("markets") or []) + (incoming.get("markets") or []):
        ticker = str(market.get("ticker") or "").strip()
        if not ticker:
            continue
        old = markets.get(ticker, {})
        markets[ticker] = {**old, **{k: v for k, v in market.items()
                                    if v not in (None, "", [], {})}}
    if "markets" in target or "markets" in incoming:
        merged["markets"] = list(markets.values())
    return merged


def normalize_events(events_by_series):
    """Deduplicate event tickers and market tickers across every selected series."""
    canonical = {}
    series_for = {}
    for series in sorted(events_by_series):
        for event in events_by_series.get(series) or []:
            ticker = _ticker(event)
            if not ticker:
                continue
            row = dict(event)
            row.setdefault("series_ticker", series)
            if ticker in canonical:
                canonical[ticker] = _merge_event(canonical[ticker], row)
            else:
                canonical[ticker] = row
                series_for[ticker] = series
    # A market ticker must have exactly one owner, even if malformed upstream
    # data repeats it across two distinct event tickers.
    seen_markets = set()
    result = {}
    for ticker in sorted(canonical):
        event = canonical[ticker]
        series = series_for[ticker]
        markets = []
        for market in event.get("markets") or []:
            mt = str(market.get("ticker") or "").strip()
            if mt and mt not in seen_markets:
                seen_markets.add(mt)
                markets.append(market)
        event["markets"] = markets
        result.setdefault(series, []).append(event)
    return result


def _scan_partition(scanner_id, role, events, now):
    stats = {"scanner": scanner_id, "role": role, "events": 0, "active_markets": 0,
             "priced_markets": 0, "missing_tickers": 0, "missing_quotes": 0,
             "live_events": 0, "live_watch_markets": []}
    for event in events:
        stats["events"] += 1
        status = str(event.get("status") or event.get("event_status") or
                     event.get("state") or "").strip().lower().replace("-", "_")
        live = status in {"live", "in_progress", "inplay", "in_play", "playing"}
        if live:
            stats["live_events"] += 1
        for market in event.get("markets") or []:
            if str(market.get("status") or "").lower() not in ("active", "open"):
                continue
            ticker = str(market.get("ticker") or "").strip()
            if not ticker:
                stats["missing_tickers"] += 1
                continue
            stats["active_markets"] += 1
            bid = market.get("yes_bid")
            ask = market.get("yes_ask")
            if bid is not None and ask is not None:
                stats["priced_markets"] += 1
            else:
                stats["missing_quotes"] += 1
            if live:
                stats["live_watch_markets"].append({
                    "ticker": ticker, "event_ticker": _ticker(event),
                    "series": event.get("series_ticker"), "status": status,
                    "yes_bid": bid, "yes_ask": ask, "observed_ts": int(now),
                    "execution_eligible": False,
                    "reason": "live scanner observation only; existing live-sports model is not validated",
                })
    return stats


def scan_fleet(events_by_series, now=None, workers=8):
    """Deduplicate once, then fan the canonical event list across eight scanners."""
    now = int(now or time.time())
    workers = max(1, min(8, int(workers)))
    normalized = normalize_events(events_by_series)
    events = [event for series in sorted(normalized) for event in normalized[series]]
    lanes = [[] for _ in range(workers)]
    for event in events:
        key = _ticker(event)
        slot = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % workers
        lanes[slot].append(event)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda args: _scan_partition(*args),
            [(i + 1, SCANNER_ROLES[i % len(SCANNER_ROLES)], lane, now)
             for i, lane in enumerate(lanes)]))
    live = {}
    for result in results:
        for row in result["live_watch_markets"]:
            live.setdefault(row["ticker"], row)
    summary = {
        "ts": now, "ok": True, "scanner_count": workers,
        "events_unique": len(events),
        "markets_unique": sum(len(e.get("markets") or []) for e in events),
        "live_events_seen": sum(r["live_events"] for r in results),
        "live_markets_observed": len(live),
        "scanners": [{k: v for k, v in r.items() if k != "live_watch_markets"}
                     for r in results],
        "execution_enabled": False,
    }
    return normalized, summary, list(live.values())
