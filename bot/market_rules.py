"""Compact, fail-closed summary of Kalshi contract settlement rules.

Inspired by read-only contract-rule tooling (including MCP implementations): retain
the exact source text for audit, extract only bounded facts for model context, and
never let an LLM override missing or contradictory settlement terms.
"""
from __future__ import annotations

import re


_RULE_FIELDS = (
    "rules_primary", "rules_secondary", "early_close_condition",
    "settlement_value", "settlement_source", "custom_strike",
    # Read-only MCP/PDF extractors sometimes expose consolidated text instead
    # of Kalshi's native rules_primary/rules_secondary fields.
    "rules_text", "rules_pdf_text", "contract_terms_text",
    # Conservative aliases used by third-party read-only MCP responses.
    "rules", "contract_terms", "series_prohibitions", "prohibitions",
)


def _unwrap_market(payload):
    """Accept native Kalshi market objects and common read-only tool envelopes.

    Only unwrap known response containers. Inner market fields take precedence,
    while useful outer rule metadata is retained. This is normalization only:
    it does not fetch PDFs or treat a third-party summary as authoritative.
    """
    if not isinstance(payload, dict):
        return {}
    market = dict(payload)
    for key in ("result", "data", "market"):
        nested = market.get(key)
        if isinstance(nested, dict):
            market = {**market, **nested}
    return market


def summarize_rules(market):
    """Return bounded, auditable settlement context from a market/tool payload.

    This helper does not fetch data, authorize orders, or infer that absent terms
    are safe. It accepts direct Kalshi market lookups and common read-only MCP
    response wrappers, including consolidated contract/PDF text when supplied.
    """
    market = _unwrap_market(market)
    terms = {}
    for key in _RULE_FIELDS:
        value = market.get(key)
        if isinstance(value, (dict, list, tuple)):
            value = str(value)
        value = str(value or "").strip()
        if value:
            terms[key] = value[:3000]

    combined = " ".join(terms.values())
    lower = combined.lower()
    missing = []
    if not any(terms.get(key) for key in (
        "rules_primary", "settlement_source", "rules_text", "rules_pdf_text",
        "contract_terms_text", "rules", "contract_terms",
    )):
        missing.append("primary settlement rules/source not present in supplied market data")
    flags = []
    patterns = (
        ("early_close", r"early close|close early|may close before"),
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
        "note": "Extracted from supplied fields only; verify official contract terms before relying on them."
    }
