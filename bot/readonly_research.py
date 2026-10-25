"""Read-only MCP-style research facade over the EXISTING KalshiClient.

Adapted concepts from 9crusher/mcp-server-kalshi and BrainDAO/mcp-kalshi:
contract rules, market discovery, price normalization, and order-book evidence.
This is NOT a second MCP process or an independent API client. It has no
portfolio, order, cancellation, or trading capabilities. Untrusted research
must never bypass the bot's existing execution/risk gates.
"""
import re
import time

from bot.kalshi_client import price


_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def _ticker(value):
    if not isinstance(value, str) or not _TICKER.fullmatch(value):
        raise ValueError("invalid Kalshi ticker")
    return value


class ReadOnlyKalshiResearch:
    """Small, explicit allowlist; all requests use the shared client and pacing."""

    def __init__(self, client):
        self._client = client

    def get_market_rules(self, ticker):
        ticker = _ticker(ticker)
        market = self._client.market(ticker)
        if not market or market.get("ticker") != ticker:
            return {"ticker": ticker, "status": "unavailable", "source": "kalshi"}
        series_ticker = market.get("series_ticker")
        series = self._client.series(_ticker(series_ticker)) if series_ticker else {}
        fields = ("rules_primary", "rules_secondary", "early_close_condition",
                  "settlement_sources", "settlement_source", "prohibitions",
                  "contract_terms_url", "rules_url")
        return {
            "ticker": ticker,
            "series_ticker": series_ticker,
            "source": "kalshi",
            "retrieved_at": int(time.time()),
            "market_rules": {k: market[k] for k in fields if market.get(k) is not None},
            "series_rules": {k: series[k] for k in fields if series.get(k) is not None},
            "status": "available" if market.get("rules_primary") or market.get("rules_secondary") else "incomplete",
            "warning": "Metadata only; verify the official contract PDF and settlement source before trading.",
        }

    def get_market_snapshot(self, ticker, depth=10):
        ticker = _ticker(ticker)
        if type(depth) is not int or not 1 <= depth <= 100:
            raise ValueError("depth must be an integer from 1 to 100")
        market = self._client.market(ticker)
        if not market or market.get("ticker") != ticker:
            return {"ticker": ticker, "status": "unavailable", "source": "kalshi"}
        book = self._client.orderbook(ticker, depth=depth)
        return {
            "ticker": ticker,
            "source": "kalshi",
            "retrieved_at": int(time.time()),
            "status": "available",
            "yes_bid_dollars": price(market, "yes_bid"),
            "yes_ask_dollars": price(market, "yes_ask"),
            "no_bid_dollars": price(market, "no_bid"),
            "no_ask_dollars": price(market, "no_ask"),
            "orderbook_bids_dollars": book,
            "warning": "Orderbook sides are bids, not executable asks; prices may change before execution.",
        }

    def get_series(self, ticker):
        return self._client.series(_ticker(ticker))

    def get_market(self, ticker):
        return self._client.market(_ticker(ticker))

    def get_recent_trades(self, ticker, limit=100):
        ticker = _ticker(ticker)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500")
        return self._client.trades(ticker, limit=limit)
