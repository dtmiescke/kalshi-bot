"""Read-only MCP-inspired research facade over the existing KalshiClient.

No separate exchange session, credentials, database, or order-writing tools.
This adapter is intentionally *not* a generic MCP tool executor: callers get
only the explicitly enumerated read operations below.
"""
from __future__ import annotations

import re
import time

from bot.contract_pdf import load_series_contract_pdf

_TICKER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _ticker(value):
    value = str(value or "").strip()
    if not _TICKER.fullmatch(value):
        raise ValueError("invalid Kalshi ticker")
    return value


class KalshiResearchTools:
    """Expose bounded market research using the bot's shared client and cache."""

    def __init__(self, client, store=None):
        self.client = client
        self.store = store

    def market(self, ticker):
        return self.client.market(_ticker(ticker))

    def series(self, ticker):
        return self.client.series(_ticker(ticker))

    def orderbook(self, ticker, depth=10):
        depth = int(depth)
        if not 1 <= depth <= 100:
            raise ValueError("depth must be between 1 and 100")
        return self.client.orderbook(_ticker(ticker), depth=depth)

    def recent_trades(self, ticker, limit=100):
        limit = int(limit)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self.client.trades(_ticker(ticker), limit=limit)

    def sports_events(self, series_ticker):
        return self.client.sports_events(_ticker(series_ticker))

    def contract_rules(self, series_ticker, now=None):
        """Official series metadata plus optional bounded PDF evidence.

        Missing PDF evidence stays missing; no fabricated settlement terms.
        """
        ticker = _ticker(series_ticker)
        metadata = self.client.series(ticker)
        if not isinstance(metadata, dict):
            return {"series_ticker": ticker, "metadata": {}, "pdf": None}
        pdf = None
        if self.store is not None:
            pdf = load_series_contract_pdf(
                self.client, self.store, ticker,
                int(time.time()) if now is None else int(now),
                series_metadata=metadata,
            )
        return {"series_ticker": ticker, "metadata": metadata, "pdf": pdf}
