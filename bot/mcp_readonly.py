"""Read-only MCP-style research facade over the existing Kalshi client.

This is intentionally NOT a second MCP server or execution client. It exposes
small, bounded research operations for future tool integrations while sharing
KalshiClient's cache, rate limiter, price normalization, and credentials.
No order creation, cancellation, or portfolio mutation is reachable here.
"""
from __future__ import annotations

import re

from bot.kalshi_client import price

_TICKER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def _ticker(value):
    value = str(value or "").strip()
    if not _TICKER.fullmatch(value):
        raise ValueError("invalid Kalshi ticker")
    return value


class KalshiResearchTools:
    """Explicit allowlist of public read-only operations."""

    def __init__(self, client):
        self._client = client

    def market_snapshot(self, ticker):
        ticker = _ticker(ticker)
        market = self._client.market(ticker)
        if not isinstance(market, dict):
            return {"ticker": ticker, "available": False}
        return {
            "ticker": ticker,
            "available": True,
            "title": market.get("title"),
            "status": market.get("status"),
            "event_ticker": market.get("event_ticker"),
            "yes_bid": price(market, "yes_bid"),
            "yes_ask": price(market, "yes_ask"),
            "no_bid": price(market, "no_bid"),
            "no_ask": price(market, "no_ask"),
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
        }

    def orderbook_snapshot(self, ticker, depth=10):
        ticker = _ticker(ticker)
        depth = int(depth)
        if not 1 <= depth <= 20:
            raise ValueError("depth must be between 1 and 20")
        return {"ticker": ticker, "depth": depth,
                "bids": self._client.orderbook(ticker, depth=depth)}

    def series_rules(self, series_ticker):
        series_ticker = _ticker(series_ticker)
        series = self._client.series(series_ticker)
        if not isinstance(series, dict):
            return {"series_ticker": series_ticker, "available": False}
        return {
            "series_ticker": series_ticker,
            "available": True,
            "title": series.get("title"),
            "settlement_sources": series.get("settlement_sources"),
            "contract_terms_url": series.get("contract_terms_url") or series.get("contract_url"),
            "rules": series.get("rules"),
        }

    def contract_pdf_evidence(self, series_ticker, store, now):
        """Reuse the project's trusted-host, size-bounded, cached PDF reader."""
        from bot.contract_pdf import load_series_contract_pdf
        return load_series_contract_pdf(
            self._client, store, _ticker(series_ticker), now
        )
