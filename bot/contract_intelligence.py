"""Read-only Kalshi contract intelligence using the existing guarded API client.

Inspired by contract-research MCP tools; deliberately no MCP process, credentials,
order methods, external fetches, or separate market-data collector.
"""
from datetime import datetime, timezone
import re

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def contract_context(client, ticker):
    """Return official market/series evidence, with unknown fields left unknown.

    Only invokes existing KalshiClient.market() and .series() GET methods.
    A PDF URL is provenance, not parsed rules or proof of settlement.
    """
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise ValueError("invalid market ticker")
    market = client.market(ticker)
    if not isinstance(market, dict) or not market:
        raise ValueError("market data unavailable")
    series_ticker = market.get("series_ticker")
    if not series_ticker:
        series_ticker = None
    series = {}
    if series_ticker:
        if not isinstance(series_ticker, str) or not _TICKER.fullmatch(series_ticker):
            raise ValueError("invalid series ticker in market response")
        series = client.series(series_ticker)
        if not isinstance(series, dict):
            series = {}
    return {
        "source": "kalshi_trade_api_v2",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "market_ticker": ticker,
        "series_ticker": series_ticker,
        "title": market.get("title"),
        "subtitle": market.get("subtitle"),
        "yes_sub_title": market.get("yes_sub_title"),
        "no_sub_title": market.get("no_sub_title"),
        "rules_primary": market.get("rules_primary"),
        "rules_secondary": market.get("rules_secondary"),
        "settlement_sources": series.get("settlement_sources"),
        "contract_url": series.get("contract_url"),
        "additional_prohibitions": series.get("additional_prohibitions"),
        "status": market.get("status"),
        "close_time": market.get("close_time"),
        "expiration_time": market.get("expiration_time"),
        "confidence": "official_metadata_only",
        "rules_pdf_verified": False,
    }
