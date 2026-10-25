"""Read-only Kalshi research adapter inspired by third-party MCP tool catalogs.

Reuses the existing KalshiClient and contract-PDF loader. This module has no
order placement, cancellation, account credentials, or dynamic tool execution.
All responses are evidence only; the trading engine retains decision authority.
"""
from __future__ import annotations

import re
import time

from bot.contract_pdf import load_series_contract_pdf
from bot.kalshi_client import price

_TICKER = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_SERIES = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def _ticker(value, pattern=_TICKER):
    text = str(value or "").strip()
    if not pattern.fullmatch(text):
        raise ValueError("invalid Kalshi ticker")
    return text


def _market_payload(payload):
    if not isinstance(payload, dict):
        return {}
    return payload.get("market", payload) if isinstance(payload.get("market", payload), dict) else {}


def market_rules_snapshot(client, ticker, *, store=None, now=None, include_pdf=False):
    """Return cited settlement evidence, never a trade instruction.

    PDF retrieval is opt-in and uses the existing allowlist, size cap and cache.
    Missing fields stay None rather than being inferred from market prices.
    """
    ticker = _ticker(ticker)
    market = _market_payload(client.market(ticker))
    if not market:
        return {"ticker": ticker, "available": False, "source": "kalshi_api"}
    series_ticker = market.get("series_ticker")
    if series_ticker:
        series_ticker = _ticker(series_ticker, _SERIES)
    result = {
        "ticker": ticker,
        "available": True,
        "source": "kalshi_api",
        "series_ticker": series_ticker,
        "rules_primary": market.get("rules_primary"),
        "rules_secondary": market.get("rules_secondary"),
        "result_source": market.get("result_source"),
        "settlement_source": market.get("settlement_source"),
        "close_time": market.get("close_time"),
        "expiration_time": market.get("expiration_time"),
        "early_close_condition": market.get("early_close_condition"),
        "yes_bid_dollars": price(market, "yes_bid"),
        "yes_ask_dollars": price(market, "yes_ask"),
        "no_bid_dollars": price(market, "no_bid"),
        "no_ask_dollars": price(market, "no_ask"),
        "observed_at": int(time.time() if now is None else now),
    }
    if include_pdf and store is not None and series_ticker:
        result["contract_pdf"] = load_series_contract_pdf(
            client, store, series_ticker, result["observed_at"])
    return result


def orderbook_snapshot(client, ticker, depth=10, *, now=None):
    """One bounded read using existing price-unit normalization and pacing."""
    ticker = _ticker(ticker)
    depth = int(depth)
    if not 1 <= depth <= 20:
        raise ValueError("depth must be 1..20")
    book = client.orderbook(ticker, depth=depth)
    return {
        "ticker": ticker,
        "source": "kalshi_api",
        "observed_at": int(time.time() if now is None else now),
        "yes_bids": list(book.get("yes", []))[:depth],
        "no_bids": list(book.get("no", []))[:depth],
        "note": "Bids only; opposite-side ask is the complement of the other bid, not a quoted ask.",
    }


def recent_trades_snapshot(client, ticker, limit=100, *, now=None):
    """Bounded public prints, never a portfolio or order-write operation."""
    ticker = _ticker(ticker)
    limit = int(limit)
    if not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    return {
        "ticker": ticker,
        "source": "kalshi_api",
        "observed_at": int(time.time() if now is None else now),
        "trades": client.trades(ticker, limit=limit)[:limit],
    }
