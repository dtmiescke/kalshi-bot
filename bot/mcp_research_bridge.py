"""Read-only MCP-inspired research bridge over the EXISTING KalshiClient.

Do not connect a second authenticated trading client or expose portfolio/order
mutations to LLM tool callers. All requests use KalshiClient's shared pacing,
retry-after handling, and normal market-data URL. Results are untrusted research
evidence, never execution authorization.
"""
from .kalshi_client import price

ALLOWED_TOOLS = frozenset({
    "market_details", "series_rules", "orderbook_snapshot",
})


class ResearchBridge:
    """Minimal explicit dispatch surface; no dynamic method invocation."""

    def __init__(self, data_client):
        self._data = data_client

    def call(self, tool, arguments):
        if tool not in ALLOWED_TOOLS:
            raise ValueError("research tool not permitted")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        ticker = arguments.get("ticker")
        if not isinstance(ticker, str) or not ticker or len(ticker) > 128:
            raise ValueError("invalid ticker")
        # Reject path/query injection before calling the existing client.
        if not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in ticker):
            raise ValueError("invalid ticker")
        if tool == "series_rules":
            if set(arguments) != {"ticker"}:
                raise ValueError("unsupported arguments")
            series = self._data.series(ticker)
            return {
                "ticker": ticker,
                "settlement_sources": series.get("settlement_sources"),
                "additional_prohibitions": series.get("additional_prohibitions"),
                "rules_primary": series.get("rules_primary"),
                "rules_secondary": series.get("rules_secondary"),
                "contract_url": series.get("contract_url"),
                "source": "kalshi_series",
            }
        if tool == "market_details":
            if set(arguments) != {"ticker"}:
                raise ValueError("unsupported arguments")
            market = self._data.market(ticker)
            return {
                "ticker": ticker,
                "event_ticker": market.get("event_ticker"),
                "status": market.get("status"),
                "yes_bid_dollars": price(market, "yes_bid"),
                "yes_ask_dollars": price(market, "yes_ask"),
                "no_bid_dollars": price(market, "no_bid"),
                "no_ask_dollars": price(market, "no_ask"),
                "source": "kalshi_market",
            }
        if set(arguments) - {"ticker", "depth"}:
            raise ValueError("unsupported arguments")
        depth = arguments.get("depth", 10)
        if type(depth) is not int or not 1 <= depth <= 20:
            raise ValueError("depth must be an integer from 1 to 20")
        return {
            "ticker": ticker,
            "depth": depth,
            "bids": self._data.orderbook(ticker, depth=depth),
            "source": "kalshi_orderbook",
        }
