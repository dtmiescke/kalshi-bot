"""Read-only contract research adapted from Kalshi MCP research concepts.

Uses the existing KalshiClient exclusively. No additional API client, credentials,
order methods, network downloads, or execution path are introduced.
"""
from urllib.parse import urlparse


def _safe_document_url(value):
    """Expose only HTTPS links; never fetch third-party documents implicitly."""
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value.strip()


def contract_research(client, ticker):
    """Return source-attributed rules and settlement metadata, not trading advice.

    An unknown series is reported explicitly rather than silently borrowing rules
    from a similarly named market. API errors propagate to the caller (fail closed).
    """
    ticker = str(ticker or "").strip()
    if not ticker or any(ch in ticker for ch in "/?#\\"):
        raise ValueError("invalid Kalshi market ticker")
    market = client.market(ticker)
    if not isinstance(market, dict) or not market or market.get("ticker") != ticker:
        raise ValueError("market missing or ticker mismatch")
    series_ticker = str(market.get("series_ticker") or "").strip()
    series = client.series(series_ticker) if series_ticker else {}
    if not isinstance(series, dict):
        raise ValueError("invalid series response")
    if series_ticker and series.get("ticker") not in (None, series_ticker):
        raise ValueError("series ticker mismatch")
    rules = {
        "primary": market.get("rules_primary") or None,
        "secondary": market.get("rules_secondary") or None,
        "early_close_condition": market.get("early_close_condition") or None,
        "settlement_source": market.get("settlement_source") or series.get("settlement_source") or None,
        "series_restrictions": series.get("restrictions") or None,
    }
    document_urls = []
    for obj in (market, series):
        for key in ("rules_pdf_url", "contract_terms_url", "contract_url"):
            url = _safe_document_url(obj.get(key))
            if url and url not in document_urls:
                document_urls.append(url)
    return {
        "ticker": ticker,
        "series_ticker": series_ticker or None,
        "status": market.get("status"),
        "rules": rules,
        "document_urls": document_urls,
        "source": "Kalshi official market/series API via existing KalshiClient",
        "complete": bool(rules["primary"] and rules["settlement_source"]),
        "warning": None if rules["primary"] and rules["settlement_source"]
                   else "Incomplete official rules or settlement source; do not infer contract terms.",
    }
