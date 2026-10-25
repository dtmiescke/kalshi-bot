"""Read-only Kalshi research facade inspired by MCP research tools.

This is NOT an MCP server and deliberately has no trading, account, or
portfolio methods. It reuses the application's KalshiClient so rate limits,
authentication policy and market caches are not duplicated.
"""
import re
from .kalshi_client import price

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _ticker(value):
    ticker = str(value or "").strip()
    if not _TICKER.fullmatch(ticker):
        raise ValueError("invalid Kalshi ticker")
    return ticker


class ReadOnlyKalshiResearch:
    """Narrow capability wrapper; never expose the underlying client to an LLM."""

    def __init__(self, client):
        self._client = client

    def market_snapshot(self, ticker):
        ticker = _ticker(ticker)
        market = self._client.market(ticker)
        if not isinstance(market, dict) or not market:
            return None
        return {
            "source": "kalshi", "ticker": ticker,
            "title": market.get("title"),
            "status": market.get("status"),
            "event_ticker": market.get("event_ticker"),
            "series_ticker": market.get("series_ticker"),
            "yes_bid": price(market, "yes_bid"),
            "yes_ask": price(market, "yes_ask"),
            "no_bid": price(market, "no_bid"),
            "no_ask": price(market, "no_ask"),
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
            "expiration_time": market.get("expiration_time"),
            "close_time": market.get("close_time"),
        }

    def series_rules(self, ticker):
        ticker = _ticker(ticker)
        series = self._client.series(ticker)
        if not isinstance(series, dict) or not series:
            return None
        return {
            "source": "kalshi", "ticker": ticker,
            "title": series.get("title"),
            "category": series.get("category"),
            "settlement_sources": series.get("settlement_sources"),
            "contract_url": series.get("contract_url"),
            "rules": series.get("rules"),
            "restrictions": series.get("restrictions"),
        }

    def orderbook_snapshot(self, ticker, depth=10):
        ticker = _ticker(ticker)
        depth = int(depth)
        if not 1 <= depth <= 20:
            raise ValueError("depth must be between 1 and 20")
        book = self._client.orderbook(ticker, depth=depth)
        return {
            "source": "kalshi", "ticker": ticker,
            "yes_bids": list(book.get("yes", []))[:depth],
            "no_bids": list(book.get("no", []))[:depth],
            "note": "Bids only; do not infer executable asks or fills without fresh execution checks.",
        }
