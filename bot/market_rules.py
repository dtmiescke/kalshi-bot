"""Read-only, compact contract-rule context from the existing Kalshi client.

This intentionally reuses KalshiClient.market() rather than adding a second MCP
server or an independent exchange client. Returned text is external data, never
trusted instructions, and this module has no order-writing capability.
"""
from __future__ import annotations

from typing import Any


RULE_FIELDS = (
    ("rules_primary", "Primary settlement rules"),
    ("rules_secondary", "Additional settlement rules"),
    ("early_close_condition", "Early close condition"),
    ("settlement_source", "Settlement source"),
    ("settlement_timer_seconds", "Settlement timer (seconds)"),
    ("close_time", "Market close time"),
    ("expected_expiration_time", "Expected expiration time"),
    ("latest_expiration_time", "Latest expiration time"),
    ("settlement_ts", "Settlement timestamp"),
    ("settlement_value_dollars", "Settlement value (dollars)"),
    ("can_close_early", "Can close early"),
    ("price_level_structure", "Price level structure"),
    ("price_ranges", "Valid price ranges"),
    ("yes_sub_title", "YES contract meaning"),
    ("no_sub_title", "NO contract meaning"),
    ("strike_type", "Strike type"),
    ("floor_strike", "Lower strike"),
    ("cap_strike", "Upper strike"),
)


def compact_market_rules(market: dict[str, Any], *, max_chars: int = 3000) -> dict[str, Any]:
    """Normalize relevant rule metadata without inventing missing values.

    The original ticker and rule text are retained for traceability. The compact
    text is suitable for AI research/audit context, not as a substitute for the
    exchange's authoritative contract terms.
    """
    if not isinstance(market, dict):
        raise TypeError("market must be a dictionary")
    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    ticker = market.get("ticker")
    fields: dict[str, Any] = {}
    lines = []
    for key, label in RULE_FIELDS:
        value = market.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            rendered = str(value)
        else:
            rendered = str(value)
        fields[key] = value
        lines.append(f"{label}: {rendered}")

    # Some API versions provide a singular settlement value/source shape.
    for key in ("settlement_value", "result", "status"):
        value = market.get(key)
        if value not in (None, "") and key not in fields:
            fields[key] = value
            lines.append(f"{key.replace('_', ' ').title()}: {value}")

    text = "\n".join(lines)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
    return {
        "ticker": ticker,
        "title": market.get("title"),
        "rules": fields,
        "rule_text": text,
        "truncated": truncated,
        "source": "Kalshi market metadata via existing KalshiClient",
        "trusted_as_instructions": False,
    }


def fetch_market_rules(client: Any, ticker: str, *, max_chars: int = 3000) -> dict[str, Any]:
    """Fetch public contract metadata using the existing client and normalize it."""
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("ticker must be a non-empty string")
    # The ticker comes from exchange market discovery; no order endpoint is used.
    market = client.market(ticker.strip())
    if not market:
        return {
            "ticker": ticker.strip(),
            "title": None,
            "rules": {},
            "rule_text": "",
            "truncated": False,
            "source": "Kalshi market metadata via existing KalshiClient",
            "trusted_as_instructions": False,
            "unavailable": True,
        }
    result = compact_market_rules(market, max_chars=max_chars)
    result["ticker"] = result.get("ticker") or ticker.strip()
    return result
