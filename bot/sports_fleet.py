"""Eight bounded sports market scanners sharing one canonical event stream.

Scanners are read-only observers. They never place orders or duplicate market writes;
the existing Engine remains the single strategy/risk/execution authority.
"""
import time
from concurrent.futures import ThreadPoolExecutor

from .event_clock import event_start_ts


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
        if not isinstance(market, dict):
            continue
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
            if not isinstance(event, dict):
                continue
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
            if not isinstance(market, dict):
                continue
            mt = str(market.get("ticker") or "").strip()
            if mt and mt not in seen_markets:
                seen_markets.add(mt)
                markets.append(market)
        event["markets"] = markets
        result.setdefault(series, []).append(event)
    return result


def _scan_event(event, now, role):
    """Role-specific read-only inspection of one canonical event."""
    stats = {"events": 1, "active_markets": 0, "priced_markets": 0,
             "missing_tickers": 0, "missing_quotes": 0, "live_events": 0,
             "missing_start_times": 0, "crossed_quotes": 0, "invalid_quotes": 0,
             "wide_spreads": 0, "markets_with_volume": 0,
             "markets_with_open_interest": 0, "missing_event_titles": 0,
             "missing_team_labels": 0, "duplicate_market_tickers": 0,
             "live_watch_markets": []}
    status = str(event.get("status") or event.get("event_status") or
                 event.get("state") or "").strip().lower().replace("-", "_")
    live = status in {"live", "in_progress", "inplay", "in_play", "playing"}
    start = event.get("start_time") or event.get("commence_time") or event.get("date")
    if role in ("schedule_and_status", "live_state_watch") and live:
        stats["live_events"] = 1
    # Kalshi occurrence time is authoritative when event-level start fields are absent.
    if role == "schedule_and_status" and not (start or event_start_ts(event)):
        stats["missing_start_times"] = 1
    if role == "matchup_evidence_watch" and not (event.get("title") or event.get("subtitle")):
        stats["missing_event_titles"] = 1
    seen = set()
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        if str(market.get("status") or "").lower() not in ("active", "open"):
            continue
        ticker = str(market.get("ticker") or "").strip()
        if not ticker:
            if role in ("market_coverage", "independent_sanity_check"):
                stats["missing_tickers"] += 1
            continue
        if ticker in seen and role == "independent_sanity_check":
            stats["duplicate_market_tickers"] += 1
        seen.add(ticker)
        stats["active_markets"] += 1
        # Support legacy cents plus Kalshi's newer dollar/fixed-point fields.
        bid = market.get("yes_bid_dollars", market.get("yes_bid_fp", market.get("yes_bid")))
        ask = market.get("yes_ask_dollars", market.get("yes_ask_fp", market.get("yes_ask")))
        if role == "liquidity_watch":
            stats["markets_with_volume"] += int(market.get("volume") is not None)
            stats["markets_with_open_interest"] += int(market.get("open_interest") is not None)
        if role == "matchup_evidence_watch" and not (
                market.get("yes_sub_title") or market.get("subtitle") or market.get("title")):
            stats["missing_team_labels"] += 1
        if bid is None or ask is None:
            if role == "quote_quality":
                stats["missing_quotes"] += 1
        else:
            try:
                b, a = float(bid), float(ask)
                if "yes_bid_dollars" not in market and "yes_bid_fp" not in market and b > 1:
                    b /= 100.0
                if "yes_ask_dollars" not in market and "yes_ask_fp" not in market and a > 1:
                    a /= 100.0
                if role == "quote_quality":
                    stats["priced_markets"] += 1
                    if not (0 <= b <= 1 and 0 <= a <= 1):
                        stats["invalid_quotes"] += 1
                    elif b > a:
                        stats["crossed_quotes"] += 1
                if role == "price_dislocation_watch" and 0 <= b <= a <= 1 and a - b >= 0.20:
                    stats["wide_spreads"] += 1
                if role == "independent_sanity_check" and not (0 <= b <= a <= 1):
                    stats["invalid_quotes"] += 1
            except (TypeError, ValueError):
                if role in ("quote_quality", "independent_sanity_check"):
                    stats["invalid_quotes"] += 1
        if role == "live_state_watch" and live:
            stats["live_watch_markets"].append({
                "ticker": ticker, "event_ticker": _ticker(event),
                "series": event.get("series_ticker"), "status": status,
                "yes_bid": bid, "yes_ask": ask, "observed_ts": int(now),
                "execution_eligible": False,
                "reason": "observation only; live-sports execution remains disabled pending validation",
            })
    return stats


def _scan_partition(scanner_id, role, events, now):
    keys = ("events", "active_markets", "priced_markets", "missing_tickers",
            "missing_quotes", "live_events", "missing_start_times", "crossed_quotes",
            "invalid_quotes", "wide_spreads", "markets_with_volume",
            "markets_with_open_interest", "missing_event_titles", "missing_team_labels",
            "duplicate_market_tickers")
    stats = {"scanner": scanner_id, "role": role, **{key: 0 for key in keys},
             "live_watch_markets": [], "scan_errors": 0, "error_samples": []}
    for event in events:
        try:
            row = _scan_event(event, now, role)
        except Exception:
            # One bounded retry for malformed/transient payloads; isolate a bad
            # event so it cannot stop the other specialist scanners.
            try:
                row = _scan_event(dict(event), now, role)
            except Exception as exc:
                stats["scan_errors"] += 1
                if len(stats["error_samples"]) < 5:
                    stats["error_samples"].append(
                        {"event_ticker": _ticker(event), "error": type(exc).__name__})
                continue
        for key in keys:
            stats[key] += row[key]
        stats["live_watch_markets"].extend(row["live_watch_markets"])
    return stats

def scan_fleet(events_by_series, now=None, workers=8):
    """Deduplicate once, then fan the canonical event list across eight scanners."""
    now = int(now or time.time())
    workers = max(1, min(8, int(workers)))
    normalized = normalize_events(events_by_series)
    events = [event for series in sorted(normalized) for event in normalized[series]]
    # Every specialist sees the same canonical event universe; this is deliberate
    # analysis fan-out, not duplicated storage or duplicate execution authority.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda args: _scan_partition(*args),
            [(i + 1, SCANNER_ROLES[i % len(SCANNER_ROLES)], events, now)
             for i in range(workers)]))
    live = {}
    for result in results:
        for row in result["live_watch_markets"]:
            live.setdefault(row["ticker"], row)
    series_coverage = {}
    active_markets = active_with_quotes = active_missing_quotes = events_without_markets = 0
    events_with_clock = 0
    for series in sorted(normalized):
        rows = normalized[series]
        series_active = series_priced = series_missing_quotes = 0
        for event in rows:
            markets = event.get("markets") or []
            if not markets:
                events_without_markets += 1
            if event_start_ts(event) is not None or event.get("start_time") or event.get("commence_time") or event.get("date"):
                events_with_clock += 1
            for market in markets:
                if str(market.get("status") or "").lower() not in ("active", "open"):
                    continue
                series_active += 1
                bid = market.get("yes_bid_dollars", market.get("yes_bid_fp", market.get("yes_bid")))
                ask = market.get("yes_ask_dollars", market.get("yes_ask_fp", market.get("yes_ask")))
                if bid is not None and ask is not None:
                    series_priced += 1
                else:
                    series_missing_quotes += 1
        active_markets += series_active
        active_with_quotes += series_priced
        active_missing_quotes += series_missing_quotes
        series_coverage[series] = {
            "events": len(rows), "active_markets": series_active,
            "active_markets_with_quotes": series_priced,
            "active_markets_missing_quotes": series_missing_quotes,
        }
    summary = {
        "ts": now, "ok": sum(r.get("scan_errors", 0) for r in results) == 0, "scanner_count": workers,
        "events_unique": len(events),
        "events_with_start_time": events_with_clock,
        "events_missing_start_time": max(0, len(events) - events_with_clock),
        "events_without_markets": events_without_markets,
        "markets_unique": sum(len(e.get("markets") or []) for e in events),
        "active_markets": active_markets,
        "active_markets_with_quotes": active_with_quotes,
        "active_markets_missing_quotes": active_missing_quotes,
        "series_coverage": series_coverage,
        "live_events_seen": sum(r["live_events"] for r in results if r["role"] == "live_state_watch"),
        "scan_errors": sum(r.get("scan_errors", 0) for r in results),
        "error_samples": [sample for r in results for sample in r.get("error_samples", [])][:10],
        "live_markets_observed": len(live),
        "scanners": [{k: v for k, v in r.items() if k != "live_watch_markets"}
                     for r in results],
        "execution_enabled": False,
    }
    return normalized, summary, list(live.values())
