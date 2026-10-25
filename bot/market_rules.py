"""Compact, fail-closed summary of Kalshi contract settlement rules.

Inspired by read-only contract-rule tooling (including MCP implementations): retain
the exact source text for audit, extract only bounded facts for model context, and
never let an LLM override missing or contradictory settlement terms.
"""
from __future__ import annotations

import re


_RULE_FIELDS = ("rules_primary", "rules_secondary", "early_close_condition",
                "settlement_value", "settlement_source", "custom_strike")


def summarize_rules(market):
    """Return bounded, auditable settlement context from a Kalshi market object.

    This helper does not fetch data, authorize orders, or infer that absent terms
    are safe. It is useful with nested market payloads and direct market lookups.
    """
    market = market if isinstance(market, dict) else {}
    terms = {key: str(market.get(key) or "").strip()[:3000]
             for key in _RULE_FIELDS if str(market.get(key) or "").strip()}
    combined = " ".join(terms.values())
    lower = combined.lower()
    missing = []
    if not terms.get("rules_primary") and not terms.get("settlement_source"):
        missing.append("primary settlement rules/source not present in supplied market data")
    flags = []
    patterns = (
        ("early_close", r"early close|close early|may close early|may close before"),
        ("settlement_exception", r"exception|subject to change|at kalshi.s discretion"),
        ("source_or_index", r"index|official source|settlement source|published by"),
        ("void_or_cancellation", r"cancelled|canceled|void|no contest|postponed|abandoned"),
        ("series_prohibition", r"prohibited|not eligible|series-specific restriction"),
    )
    for name, pattern in patterns:
        if re.search(pattern, lower):
            flags.append(name)
    # No text means unavailable, not a generic review-required result.
    if not terms:
        status = "unavailable"
    elif missing or "early_close" in flags:
        status = "review_required"
    else:
        status = "terms_found"
    return {
        "available": bool(terms),
        "status": status,
        "missing": missing,
        "flags": flags,
        "terms": terms,
        "text_chars": len(combined),
        "note": "Extracted from supplied Kalshi fields only; verify the official contract before relying on it."
    }
