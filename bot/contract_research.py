"""Read-only Kalshi contract research, inspired by external MCP rule tools.

This adapter reuses the existing KalshiClient; it never accepts credentials,
creates another HTTP client, or exposes order methods to an LLM.
"""
from urllib.parse import urlparse


def _source_url(value):
    """Accept only public HTTPS source links; do not fetch them here."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def contract_rules(client, ticker):
    """Return bounded, source-attributed rules for human/AI research only.

    No PDF download or arbitrary URL fetch is performed. Missing official
    fields stay missing rather than being inferred from market titles.
    """
    ticker = str(ticker or "").strip()
    if not ticker or len(ticker) > 128 or not all(c.isalnum() or c in "-_" for c in ticker):
        raise ValueError("invalid market ticker")
    market = client.market(ticker)
    if not isinstance(market, dict) or not market:
        return {"ticker": ticker, "status": "unavailable", "trade_eligible": False}
    series_ticker = market.get("series_ticker")
    series = {}
    if isinstance(series_ticker, str) and series_ticker and all(
        c.isalnum() or c in "-_" for c in series_ticker
    ):
        try:
            series = client.series(series_ticker) or {}
        except Exception:
            # A failed optional series lookup must not fabricate settlement terms.
            series = {}
    if not isinstance(series, dict):
        series = {}
    fields = ("rules_primary", "rules_secondary", "settlement_source",
              "settlement_sources", "early_close_condition", "close_time",
              "expiration_time", "result", "status")
    market_rules = {key: market[key] for key in fields if market.get(key) not in (None, "")}
    series_fields = ("settlement_sources", "settlement_source", "restrictions",
                     "rules", "category", "title")
    series_rules = {key: series[key] for key in series_fields if series.get(key) not in (None, "")}
    links = {}
    for name in ("contract_terms_url", "rules_url", "rules_pdf_url"):
        link = _source_url(market.get(name) or series.get(name))
        if link:
            links[name] = link
    return {
        "ticker": ticker,
        "series_ticker": series_ticker,
        "market": market_rules,
        "series": series_rules,
        "source_links": links,
        "status": "read_only",
        "trade_eligible": False,
        "warning": "Research only: verify official contract terms and freshness before any decision.",
    }
