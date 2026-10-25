"""Event-time helpers for sports-market scan prioritization."""
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
    event = event if isinstance(event, dict) else {}
    occurrence, expiration = [], []
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        if str(market.get("status") or "").lower() not in ("active", "open", ""):
            continue
        start = _parse_timestamp(market.get("occurrence_datetime"))
        if start is not None:
            occurrence.append(start)
            continue
        end = _parse_timestamp(market.get("expected_expiration_time"))
        if end is not None:
            expiration.append(end)
    if occurrence:
        return min(occurrence)
    for key in ("start_time", "commence_time", "scheduled_start_time", "date"):
        value = _parse_timestamp(event.get(key))
        if value is not None:
            return value
    return min(expiration) if expiration else None
