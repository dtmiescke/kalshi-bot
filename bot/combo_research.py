"""Read-only Kalshi combo discovery.

The current trading client does not implement Kalshi's combo RFQ/quote lifecycle.
This module intentionally discovers and records actual multivariate events only;
it never fabricates a parlay price or submits an order.
"""
import time


def scan(client, store, max_events=500):
    now = int(time.time())
    try:
        events = client.multivariate_events(status="open")
    except Exception as exc:
        result = {"enabled": True, "ok": False, "ts": now, "count": 0,
                  "execution_enabled": False, "error": str(exc)[:240]}
        store.set_kv("combo_research_health", result)
        return result

    rows = []
    for event in events[:max(1, min(2000, int(max_events)))]:
        ticker = event.get("event_ticker") or event.get("ticker")
        if not ticker:
            continue
        markets = event.get("markets") or []
        rows.append({
            "event_ticker": ticker,
            "title": event.get("title") or event.get("sub_title") or "",
            "series_ticker": event.get("series_ticker"),
            "market_count": len(markets),
            "market_tickers": [m.get("ticker") for m in markets if m.get("ticker")][:50],
            "status": event.get("status"),
            "close_time": event.get("close_time"),
        })
    result = {
        "enabled": True, "ok": True, "ts": now, "count": len(rows),
        "events": rows, "execution_enabled": False,
        "reason": "Discovery only: combo RFQ quotes, joint-probability validation, fees, fill state, and settlement reconciliation must be implemented and tested before order placement.",
    }
    store.set_kv("combo_research_health", result)
    return result
