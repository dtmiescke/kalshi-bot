"""Compact, fail-closed summary of Kalshi contract settlement rules.

Inspired by read-only contract-rule tooling (including MCP implementations): retain
the exact source text for audit, extract only bounded facts for model context, and
never let an LLM override missing or contradictory settlement terms.
"""
from __future__ import annotations

import json
import re


_RULE_FIELDS = (
    "rules_primary", "rules_secondary", "early_close_condition",
    "settlement_value", "settlement_source", "custom_strike",
    "rules_text", "rules_pdf_text", "contract_terms_text",
    "rules", "contract_terms", "series_prohibitions", "prohibitions",
    "settlement_sources", "additional_prohibitions",
)
_MAX_DEPTH = 8
_MAX_NODES = 64
_MAX_TEXT_BLOCKS = 8
_MAX_PLAIN_TEXT_CHARS = 6000
_MAX_JSON_ENVELOPE_CHARS = 20000


def _unwrap_market(payload):
    """Normalize native Kalshi objects and nested read-only MCP envelopes.

    MCP SDKs may put structuredContent, JSON text blocks, and result/data/market
    envelopes at multiple levels. Walk only those known containers with strict
    depth, node, and text bounds. Any MCP error or resource-limit violation fails
    closed. Plain text is retained only as bounded evidence and cannot satisfy
    the native settlement-source gate.
    """
    if not isinstance(payload, dict):
        return {}

    envelope_keys = {"structuredContent", "result", "data", "market", "content"}
    merged = {}
    pending = [(payload, 0)]
    visited = set()
    plain_text = []
    plain_text_chars = 0
    text_blocks = 0
    nodes_seen = 0

    while pending:
        node, depth = pending.pop(0)
        if not isinstance(node, dict):
            continue
        if depth > _MAX_DEPTH:
            return {}
        nodes_seen += 1
        if nodes_seen > _MAX_NODES:
            return {}

        identity = id(node)
        if identity in visited:
            continue
        visited.add(identity)

        if node.get("isError") is True:
            return {}

        merged.update({k: v for k, v in node.items() if k not in envelope_keys})

        for key in ("structuredContent", "result", "data", "market"):
            nested = node.get(key)
            if isinstance(nested, dict):
                pending.append((nested, depth + 1))
            elif isinstance(nested, str):
                # Some adapters serialize the result/data object instead of
                # returning structuredContent. Parse only known envelope fields.
                candidate_text = nested.strip()
                if candidate_text.startswith("{") and len(candidate_text) <= _MAX_JSON_ENVELOPE_CHARS:
                    try:
                        candidate = json.loads(candidate_text)
                    except (TypeError, ValueError):
                        candidate = None
                    if isinstance(candidate, dict):
                        pending.append((candidate, depth + 1))

        blocks = node.get("content")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = str(block.get("text", "")).strip()
                if not text:
                    continue
                text_blocks += 1
                if text_blocks > _MAX_TEXT_BLOCKS:
                    return {}
                candidate = None
                if len(text) <= _MAX_JSON_ENVELOPE_CHARS:
                    try:
                        candidate = json.loads(text)
                    except (TypeError, ValueError):
                        candidate = None
                if isinstance(candidate, dict):
                    pending.append((candidate, depth + 1))
                else:
                    remaining = _MAX_PLAIN_TEXT_CHARS - plain_text_chars
                    if remaining <= 0:
                        break
                    bounded = text[:remaining]
                    plain_text.append(bounded)
                    plain_text_chars += len(bounded)

    if plain_text and not merged.get("rules_pdf_text"):
        merged["rules_pdf_text"] = "\n".join(plain_text)[:_MAX_PLAIN_TEXT_CHARS]
    return merged


def summarize_rules(market, series=None):
    """Return bounded, auditable settlement context from a market/tool payload.

    This helper does not fetch data, authorize orders, or infer that absent terms
    are safe. It accepts direct Kalshi market lookups and common read-only MCP
    response wrappers, including consolidated contract/PDF text when supplied.
    """
    market = _unwrap_market(market)
    series = _unwrap_market(series) if isinstance(series, dict) else {}
    terms = {}
    for key in _RULE_FIELDS:
        value = market.get(key)
        if isinstance(value, (dict, list, tuple)):
            value = str(value)
        value = str(value or "").strip()
        if value:
            terms[key] = value[:3000]

    for key in ("settlement_sources", "additional_prohibitions", "series_prohibitions",
                "rules_primary", "rules_secondary"):
        value = series.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (dict, list, tuple)):
            value = str(value)
        value = str(value).strip()[:3000]
        if value and key not in terms:
            terms[key] = value
        elif value and key in terms and value not in terms[key]:
            terms[key] = (terms[key] + " | series: " + value)[:3000]

    combined = " ".join(terms.values())
    lower = combined.lower()
    missing = []
    if not terms.get("rules_primary") and not terms.get("settlement_source") \
            and not terms.get("settlement_sources") \
            and not terms.get("rules_text") \
            and not terms.get("contract_terms_text") \
            and not terms.get("rules") and not terms.get("contract_terms"):
        missing.append("primary settlement rules/source not present in supplied market/series data")
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
    if (terms.get("additional_prohibitions") or terms.get("series_prohibitions")
            or terms.get("prohibitions")) and "series_prohibition" not in flags:
        flags.append("series_prohibition")

    from .contract_pdf import is_trusted_contract_url

    doc_urls = []
    for key in ("contract_terms_url", "contract_url"):
        url = str(series.get(key) or market.get(key) or "").strip()
        if is_trusted_contract_url(url) and url not in doc_urls:
            doc_urls.append(url[:1000])

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
        "note": "Extracted from supplied fields only; verify official contract terms before relying on them."
    }
