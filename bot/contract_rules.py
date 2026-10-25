"""Read-only audit of the official Kalshi market rule fields.

This deliberately consumes metadata already returned by KalshiClient. It does
not add a second API client or execution path, and it never treats a rule audit
as proof that a contract is safe to trade. Missing/ambiguous rules are surfaced
as unknown so callers can fail closed where exact settlement terms are needed.
"""
import re


def audit_rules(market):
    """Return normalized, informational rule metadata for a Kalshi market."""
    market = market if isinstance(market, dict) else {}
    primary = str(market.get("rules_primary") or "")
    secondary = str(market.get("rules_secondary") or "")
    text = " ".join(part.strip() for part in (primary, secondary) if part.strip())
    lower = text.lower()

    # Keep this conservative: only label a source when it is explicitly named.
    sources = []
    for label in ("CF Benchmarks", "Coinbase", "Kraken", "National Weather Service",
                  "NOAA", "ESPN", "SportRadar", "Dow Jones", "Bureau of Labor Statistics",
                  "Federal Reserve"):
        if label.lower() in lower and label not in sources:
            sources.append(label)

    early_close = None
    if re.search(r"early close|close early|closed early|early termination", lower):
        early_close = "mentioned"
    elif text:
        early_close = "not_detected"
    else:
        early_close = "unknown"

    prohibited = []
    for phrase in ("prohibited", "not eligible", "void", "cancelled", "canceled",
                   "suspended", "disrupted"):
        if phrase in lower:
            prohibited.append(phrase)

    return {
        "rules_present": bool(text),
        "rules_primary_present": bool(primary.strip()),
        "rules_secondary_present": bool(secondary.strip()),
        "settlement_sources_named": sources,
        "early_close_language": early_close,
        "risk_terms_to_review": prohibited,
        "audit_scope": "official market rule text only; legal PDF not independently fetched",
    }
