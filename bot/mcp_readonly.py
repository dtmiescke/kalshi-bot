"""MCP-compatible, read-only research surface over the EXISTING Kalshi client.

This module is intentionally not an MCP server and never opens a second API
connection. The caller supplies the project's existing KalshiClient instance.
Third-party MCP tools may inform research, but cannot acquire order authority.
"""
from __future__ import annotations

import re

from .market_rules import summarize_rules
from .contract_pdf import load_series_contract_pdf

_TICKER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_ALLOWED = frozenset({"market", "series", "orderbook", "rules", "rules_pdf"})


def research_tool(client, tool, ticker, *, store=None, now=None):
    """Execute only explicitly allowlisted public reads; no arbitrary dispatch.

    Results are advisory and must never be treated as permission to trade.
    Order books are sourced from the current KalshiClient (which already
    handles bid normalization, pacing and freshness).
    """
    if tool not in _ALLOWED:
        raise ValueError("unsupported or non-read-only research tool")
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise ValueError("invalid ticker")
    if tool == "market":
        return client.market(ticker)
    if tool == "series":
        return client.series(ticker)
    if tool == "orderbook":
        return client.orderbook(ticker)
    if tool == "rules_pdf":
        # Explicit opt-in to the existing bounded, trusted-host PDF loader.
        # A series ticker is required; never infer it from a market ticker.
        if store is None or now is None:
            raise ValueError("rules_pdf requires store and current timestamp")
        return load_series_contract_pdf(client, store, ticker, now)
    market = client.market(ticker)
    if not isinstance(market, dict):
        return summarize_rules({})
    series_ticker = market.get("series_ticker")
    series = (client.series(series_ticker)
              if isinstance(series_ticker, str) and _TICKER.fullmatch(series_ticker)
              else None)
    return summarize_rules(market, series)
