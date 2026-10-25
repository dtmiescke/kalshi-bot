"""Kalshi live vs historical data split (known problem #14).

Kalshi moves older settled markets, trades, candlesticks and orders to separate
historical endpoints, and the live endpoints stop returning them. This module
looks in the live data first and falls back to the archive, recording which
route was used so the health checks can count fallbacks.

Works with the bot's existing KalshiClient (it only needs `_req` and `_paged`).

Paths and response keys checked against docs.kalshi.com on 2026-10-25
(api-reference/historical: get-historical-cutoff-timestamps, get-historical-market,
get-historical-trades; all public). They are isolated in PATHS/KEYS so a Kalshi
change is a one-line fix.
"""
import time

PATHS = {
    "cutoff": "/historical/cutoff",
    "live_market": "/markets/{ticker}",
    "hist_market": "/historical/markets/{ticker}",
    "live_trades": "/markets/trades",
    "hist_trades": "/historical/trades",
}
KEYS = {"market": "market", "trades": "trades"}

_CUTOFF_CACHE = {"ts": 0.0, "data": None}
CUTOFF_TTL = 3600


class HistoryRouter:
    def __init__(self, client):
        self.client = client
        self.fallbacks = {"market": 0, "trades": 0}
        self.errors = []

    def cutoff(self):
        """Kalshi's archive cutoff info, cached for an hour. None if unavailable."""
        now = time.time()
        if _CUTOFF_CACHE["data"] is not None and now - _CUTOFF_CACHE["ts"] < CUTOFF_TTL:
            return _CUTOFF_CACHE["data"]
        try:
            data = self.client._req("GET", PATHS["cutoff"])
        except Exception as e:
            self.errors.append(f"cutoff: {e}")
            return None
        _CUTOFF_CACHE.update(ts=now, data=data)
        return data

    def _live_market(self, ticker):
        # KalshiClient.market() already unwraps the "market" key; plain clients use _req
        if hasattr(self.client, "market"):
            return self.client.market(ticker)
        return self.client._req("GET", PATHS["live_market"].format(ticker=ticker)).get(KEYS["market"])

    def market(self, ticker):
        """Market record from live data, else the archive. Returns (market, source)."""
        try:
            m = self._live_market(ticker)
            if m:
                return m, "live"
        except Exception as e:
            self.errors.append(f"live market {ticker}: {e}")
        if not hasattr(self.client, "_req"):
            return None, "missing"
        try:
            m = self.client._req("GET", PATHS["hist_market"].format(ticker=ticker)).get(KEYS["market"])
            if m:
                self.fallbacks["market"] += 1
                return m, "archive"
        except Exception as e:
            self.errors.append(f"archive market {ticker}: {e}")
        return None, "missing"

    def trades(self, ticker, min_ts=None, limit=500, max_pages=5):
        """Trades from live data plus the archive, de-duplicated by trade_id."""
        params = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        rows, seen = [], set()
        for path, label in ((PATHS["live_trades"], "live"), (PATHS["hist_trades"], "archive")):
            try:
                got = self.client._paged(path, KEYS["trades"], params, max_pages=max_pages)
            except Exception as e:
                self.errors.append(f"{label} trades {ticker}: {e}")
                continue
            if label == "archive" and got:
                self.fallbacks["trades"] += 1
            for t in got:
                tid = t.get("trade_id") or (t.get("created_time"), t.get("yes_price_dollars"), t.get("count"))
                if tid not in seen:
                    seen.add(tid)
                    rows.append(t)
        return rows

    def settlement(self, ticker):
        """Settlement info for grading: (result, expiration_value, source).

        expiration_value is "the value that was considered for the settlement"
        (e.g. the CF Benchmarks average). settlement_value_dollars is the YES
        payout ($1 or $0), not the index, so it is not used here."""
        m, source = self.market(ticker)
        if not m:
            return None, None, "missing"
        result = m.get("result")  # expected 'yes' / 'no' once settled
        return result or None, m.get("expiration_value"), source


class GradingClient:
    """What the grader calls: market(ticker) like KalshiClient, but settled
    markets Kalshi has moved to the archive are still found, so settled bets
    never go ungraded. A ticker missing everywhere raises LookupError, which
    the grader already treats as "try again later"."""

    def __init__(self, client):
        self.router = client if isinstance(client, HistoryRouter) else HistoryRouter(client)

    def market(self, ticker):
        m, _source = self.router.market(ticker)
        if not m:
            raise LookupError(f"market {ticker} not found live or in the archive")
        return m

    def health(self):
        return {"archive_fallbacks": self.router.fallbacks["market"],
                "errors": len(self.router.errors), "error_samples": self.router.errors[:5]}
