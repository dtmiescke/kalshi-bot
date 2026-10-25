"""Read-only Kalshi research adapter inspired by public MCP market tools.

Reuses the bot's KalshiClient. No credentials, subprocess, trading tool, or
second rate limiter is introduced. Output is evidence, never an order signal.
"""
from urllib.parse import urlparse


def _safe_document_url(value):
    """Accept only HTTPS URLs; reject embedded credentials and malformed hosts."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        url = urlparse(value.strip())
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return None
        return url.geturl()
    except ValueError:
        return None


def contract_research(client, ticker):
    """Return bounded contract metadata and official series rules using one client.

    Any missing official rule remains unknown. Never infer a settlement rule
    from a title, and never fetch or execute an arbitrary rules document.
    """
    ticker = str(ticker or "").strip()
    if not ticker or len(ticker) > 120 or not all(
        c.isalnum() or c in "-_" for c in ticker
    ):
        raise ValueError("invalid market ticker")
    market = client.market(ticker)
    if not isinstance(market, dict) or not market:
        return {"ticker": ticker, "status": "unavailable", "trade_ready": False}
    series_ticker = market.get("series_ticker")
    if not series_ticker:
        # Event-derived series names are not reliable; never guess.
        series_ticker = None
    series = {}
    if series_ticker:
        try:
            series = client.series(series_ticker) or {}
        except Exception:
            series = {}
    return {
        "ticker": ticker,
        "status": market.get("status", "unknown"),
        "trade_ready": False,  # research-only; not an execution permission
        "series_ticker": series_ticker,
        "market_title": market.get("title"),
        "market_subtitle": market.get("subtitle"),
        "market_rules_primary": market.get("rules_primary"),
        "market_rules_secondary": market.get("rules_secondary"),
        "series_rules": series.get("rules"),
        "settlement_sources": series.get("settlement_sources"),
        "series_restrictions": series.get("restrictions"),
        "rules_url": _safe_document_url(
            series.get("contract_url") or series.get("rules_url")
            or market.get("rules_url")
        ),
        "provenance": "kalshi_api_via_existing_client",
        "missing_rules": not bool(
            market.get("rules_primary") or series.get("rules")
        ),
    }
