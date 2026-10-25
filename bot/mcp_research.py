"""Read-only MCP-style research facade over the existing KalshiClient.

This is deliberately NOT an independent MCP trading server. All calls use the
existing client (and therefore its shared rate limiter and market-data parsing).
The allowlist excludes portfolio and every order mutation.
"""
import re

from bot.contract_pdf import load_series_contract_pdf

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class KalshiResearchTools:
    """A small, explicit, read-only capability boundary for research agents."""

    def __init__(self, client, store=None):
        self.client = client
        self.store = store

    @staticmethod
    def _ticker(value):
        if not isinstance(value, str) or not _TICKER.fullmatch(value):
            raise ValueError("invalid Kalshi ticker")
        return value

    def get_market(self, ticker):
        return self.client.market(self._ticker(ticker))

    def get_series(self, ticker):
        return self.client.series(self._ticker(ticker))

    def get_orderbook(self, ticker, depth=20):
        depth = int(depth)
        if not 1 <= depth <= 100:
            raise ValueError("depth must be between 1 and 100")
        return self.client.orderbook(self._ticker(ticker), depth=depth)

    def get_trades(self, ticker, limit=100):
        limit = int(limit)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self.client.trades(self._ticker(ticker), limit=limit)

    def get_sports_events(self, series_ticker):
        return self.client.sports_events(self._ticker(series_ticker))

    def get_contract_terms(self, series_ticker, now=None):
        """Use the existing validated PDF extractor/cache, not a second fetcher."""
        import time
        ticker = self._ticker(series_ticker)
        series = self.client.series(ticker)
        result = {"series": series, "contract_pdf": None}
        if self.store is not None:
            result["contract_pdf"] = load_series_contract_pdf(
                self.client, self.store, ticker,
                time.time() if now is None else now, series)
        return result
