"""Read-only Kalshi research interface inspired by MCP market/rules tools.

Reuses the bot's KalshiClient for pacing, auth, caching and endpoint consistency.
It intentionally exposes no portfolio mutation or order-placement capability.
All values are evidence, not permission to trade.
"""
import re

from .kalshi_client import price

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _ticker(value):
    value = str(value or "").strip()
    if not _TICKER.fullmatch(value):
        raise ValueError("invalid Kalshi ticker")
    return value


class KalshiResearch:
    """Strict allowlist of read-only research operations on an existing client."""

    def __init__(self, client):
        self._client = client

    def market(self, ticker):
        row = self._client.market(_ticker(ticker))
        return {
            "ticker": row.get("ticker"),
            "title": row.get("title"),
            "subtitle": row.get("subtitle"),
            "status": row.get("status"),
            "event_ticker": row.get("event_ticker"),
            "series_ticker": row.get("series_ticker"),
            "rules_primary": row.get("rules_primary"),
            "rules_secondary": row.get("rules_secondary"),
            "expiration_time": row.get("expiration_time"),
            "close_time": row.get("close_time"),
            "yes_bid_dollars": price(row, "yes_bid"),
            "yes_ask_dollars": price(row, "yes_ask"),
            "no_bid_dollars": price(row, "no_bid"),
            "no_ask_dollars": price(row, "no_ask"),
        }

    def rules(self, ticker):
        """Contract wording and source links only; never infer missing rules."""
        market = self.market(ticker)
        series_ticker = market.get("series_ticker")
        series = self._client.series(_ticker(series_ticker)) if series_ticker else {}
        return {
            "ticker": market["ticker"],
            "series_ticker": series_ticker,
            "rules_primary": market["rules_primary"],
            "rules_secondary": market["rules_secondary"],
            "expiration_time": market["expiration_time"],
            "close_time": market["close_time"],
            "settlement_sources": series.get("settlement_sources"),
            "contract_url": series.get("contract_url"),
            "contract_terms_url": series.get("contract_terms_url"),
            "series_restrictions": series.get("restrictions"),
            "complete": bool(market["rules_primary"] and series_ticker),
        }

    def orderbook(self, ticker, depth=10):
        depth = int(depth)
        if not 1 <= depth <= 100:
            raise ValueError("depth must be 1..100")
        return self._client.orderbook(_ticker(ticker), depth=depth)

    def trades(self, ticker, limit=100):
        limit = int(limit)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        return self._client.trades(_ticker(ticker), limit=limit)
