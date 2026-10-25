"""Compact, fail-closed summary of Kalshi market AND series settlement rules.

This adopts the useful read-only contract research pattern from Kalshi MCP servers
without introducing a second exchange client or an order-writing tool. The caller may
supply market and series payloads already obtained through the existing KalshiClient.
No terms are invented, external URLs are not fetched here, and missing data is explicit.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


_MARKET_FIELDS = (
    "rules_primary", "rules_secondary", "early_close_condition",
    "settlement_value", "settlement_source", "custom_strike",
)
_SERIES_FIELDS = (
    "settlement_sources", "additional_prohibitions", "contract_terms_url",
    "contract_url", "rules_primary", "rules_secondary",
)
_MAX_FIELD_CHARS = 3000


def _bounded_text(value):
    """Convert common API text/list/object fields into bounded audit text."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (list, tuple)):
        text = "; ".join(_bounded_text(item) for item in value if item not in (None, ""))
    elif isinstance(value, dict):
        # Preserve the meaningful keys instead of dumping opaque nested metadata.
        text = "; ".join(
            f"{key}: {_bounded_text(val)}"
            for key, val in value.items()
            if val not in (None, "", [], {})
        )
    else:
        text = str(value).strip()
    return text[:_MAX_FIELD_CHARS]


def _official_rules_url(value):
    """Only expose likely official Kalshi rule-document links as trusted references."""
    url = str(value or "").strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        return None
    is_kalshi_s3 = host.startswith("kalshi-public-docs.s3.") and host.endswith(".amazonaws.com")
    if not (host == "kalshi.com" or host.endswith(".kalshi.com") or is_kalshi_s3):
        return None
    return url[:1000]


def summarize_rules(market, series=None):
    """Return bounded, auditable settlement context from supplied Kalshi payloads.

    Args:
        market: Market object returned by the existing Kalshi API client.
        series: Optional GET /series/{ticker} object from that same client.

    The function is deliberately read-only. It does not fetch PDFs, authorize orders,
    or infer that absent terms are safe. Official document URLs are references only;
    a caller must not treat a URL as proof that its document was successfully read.
    """
    market = market if isinstance(market, dict) else {}
    series = series if isinstance(series, dict) else {}

    terms = {}
    for key in _MARKET_FIELDS:
        text = _bounded_text(market.get(key))
        if text:
            terms[key] = text
    for key in _SERIES_FIELDS:
        text = _bounded_text(series.get(key))
        if text and key not in terms:
            terms[key] = text
        elif text and key in terms and text not in terms[key]:
            # Keep market-specific wording first; add the series-level legal context.
            terms[key] = (terms[key] + " | series: " + text)[:_MAX_FIELD_CHARS]

    # Some market payloads expose settlement_sources at market level too.
    market_sources = _bounded_text(market.get("settlement_sources"))
    if market_sources and "settlement_sources" not in terms:
        terms["settlement_sources"] = market_sources

    doc_urls = []
    for key in ("contract_terms_url", "contract_url"):
        url = _official_rules_url(series.get(key) or market.get(key))
        if url and url not in doc_urls:
            doc_urls.append(url)

    combined = " ".join(terms.values())
    lower = combined.lower()
    has_source = any(terms.get(key) for key in (
        "rules_primary", "settlement_source", "settlement_sources"
    ))
    missing = []
    if not has_source:
        missing.append("primary settlement rules/source not present in supplied market or series data")

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

    # A prohibition field is itself review-worthy even if it uses unfamiliar wording.
    if terms.get("additional_prohibitions") and "series_prohibition" not in flags:
        flags.append("series_prohibition")

    if not terms:
        status = "unavailable"
    elif missing or "early_close" in flags or "series_prohibition" in flags:
        status = "review_required"
    else:
        status = "terms_found"

    return {
        "available": bool(terms),
        "status": status,
        "missing": missing,
        "flags": flags,
        "terms": terms,
        "official_rule_document_urls": doc_urls,
        "text_chars": len(combined),
        "note": (
            "Extracted only from supplied Kalshi market/series fields. Document URLs are "
            "references, not verified PDF contents; verify official terms before relying on them."
        ),
    }
