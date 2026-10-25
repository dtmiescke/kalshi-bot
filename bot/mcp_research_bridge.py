"""Read-only Kalshi research tool bridge for the existing engine.

Inspired by Kalshi MCP research workflows; does not start another server,
accept trading credentials, or expose order/portfolio mutation tools.
All reads reuse the existing KalshiClient pacing and parsing.
"""
from __future__ import annotations

import re
from .contract_pdf import load_series_contract_pdf

_TICKER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TOOLS = {
    "kalshi_market_research": "Public market metadata and settlement context",
    "kalshi_series_rules": "Official series metadata and bounded contract PDF evidence",
    "kalshi_orderbook_research": "Public bid depth normalized to dollar prices",
}


def _ticker(value):
    value = str(value or "").strip()
    if not _TICKER.fullmatch(value):
        raise ValueError("invalid ticker")
    return value


def research_tool(client, store, name, arguments, now):
    """Return a bounded research record; never execute orders.

    The caller supplies the existing KalshiClient and store. Outputs are
    untrusted evidence, not instructions or permission to place an order.
    """
    if name not in TOOLS:
        raise ValueError("unsupported research tool")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    ticker = _ticker(arguments.get("ticker"))
    if name == "kalshi_market_research":
        market = client.market(ticker)
        if not isinstance(market, dict):
            return {"source": "kalshi", "ticker": ticker, "available": False}
        keys = ("ticker", "event_ticker", "series_ticker", "title", "subtitle",
                "status", "close_time", "expiration_time", "result",
                "rules_primary", "rules_secondary", "yes_sub_title", "no_sub_title")
        return {"source": "kalshi", "ticker": ticker, "observed_at": int(now),
                "market": {k: market[k] for k in keys if k in market}}
    if name == "kalshi_series_rules":
        series = client.series(ticker)
        if not isinstance(series, dict):
            return {"source": "kalshi", "ticker": ticker, "available": False}
        keys = ("ticker", "title", "category", "contract_terms_url",
                "contract_url", "settlement_sources", "rules")
        result = {"source": "kalshi", "ticker": ticker, "observed_at": int(now),
                  "series": {k: series[k] for k in keys if k in series}}
        pdf = load_series_contract_pdf(client, store, ticker, now, series_metadata=series)
        if pdf:
            result["contract_pdf"] = pdf
        else:
            result["contract_pdf_status"] = "unavailable_or_unverified"
        return result
    depth = arguments.get("depth", 10)
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 20:
        raise ValueError("depth must be an integer from 1 to 20")
    book = client.orderbook(ticker, depth=depth)
    return {"source": "kalshi", "ticker": ticker, "observed_at": int(now),
            "book_type": "bids_only", "price_unit": "dollars",
            "book": {side: [[float(p), float(q)] for p, q in book.get(side, [])[:depth]]
                     for side in ("yes", "no")}}
