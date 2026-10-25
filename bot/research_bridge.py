"""Read-only Kalshi research bridge.

This is a narrow MCP-style tool surface backed by the EXISTING KalshiClient.
It intentionally has no portfolio, order, cancel, or arbitrary HTTP method.
The bridge does not create another API client or execution authority.
"""
import re

from bot.kalshi_client import price

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TOOLS = frozenset({"market_snapshot", "series_rules", "orderbook_snapshot"})


def _ticker(value):
    if not isinstance(value, str) or not _TICKER.fullmatch(value):
        raise ValueError("invalid ticker")
    return value


def research_tool(client, tool, ticker):
    """Dispatch one allowlisted read-only research operation.

    Pass an existing KalshiClient instance to share its pacing and caching.
    Output is untrusted market evidence, not an execution instruction.
    """
    if tool not in _TOOLS:
        raise ValueError("unsupported read-only research tool")
    ticker = _ticker(ticker)
    if tool == "market_snapshot":
        market = client.market(ticker)
        if not isinstance(market, dict):
            raise ValueError("invalid market response")
        return {
            "source": "kalshi", "tool": tool, "ticker": ticker,
            "market": market,
            "yes_bid_dollars": price(market, "yes_bid"),
            "yes_ask_dollars": price(market, "yes_ask"),
            "no_bid_dollars": price(market, "no_bid"),
            "no_ask_dollars": price(market, "no_ask"),
        }
    if tool == "series_rules":
        series = client.series(ticker)
        if not isinstance(series, dict):
            raise ValueError("invalid series response")
        return {"source": "kalshi", "tool": tool, "ticker": ticker,
                "series": series, "settlement_verified": False}
    book = client.orderbook(ticker, depth=20)
    if not isinstance(book, dict):
        raise ValueError("invalid orderbook response")
    return {"source": "kalshi", "tool": tool, "ticker": ticker,
            "orderbook": book, "freshness": "request_time_only"}


READ_ONLY_RESEARCH_TOOLS = tuple(sorted(_TOOLS))
