"""Event-time helpers for sports-market scan prioritization.

Kalshi market occurrence times are the primary source. Expected expiration is a
fallback when occurrence time is absent. Invalid/missing timestamps are ignored.
"""
from datetime import datetime


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def event_start_ts(event):
    """Return the earliest reliable occurrence time for an open market event."""
    markets = event.get("markets") or []
    occurrence = []
    expiration = []
    for market in markets:
        if market.get("status") not in ("active", "open", None):
            continue
        start = _parse_timestamp(market.get("occurrence_datetime"))
        if start is not None:
            occurrence.append(start)
            continue
        end = _parse_timestamp(market.get("expected_expiration_time"))
        if end is not None:
            expiration.append(end)
    values = occurrence or expiration
    return min(values) if values else None
