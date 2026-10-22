"""Sportsbook odds (The Odds API) and vig removal.

Sportsbooks build a margin ("vig") into their prices, so their implied
probabilities sum to more than 100%. We strip it to get fair probabilities,
then blend books with sharper books weighted higher.
"""
import math
import time

import requests

from .config import BOOK_WEIGHTS

ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"


def american_to_prob(odds):
    odds = float(odds)
    return 100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)


def decimal_to_prob(odds):
    return 1.0 / float(odds)


def devig_multiplicative(probs):
    s = sum(probs)
    return [p / s for p in probs]


def devig_power(probs, iters=60):
    """Power method: find k with sum(p_i ** k) = 1. Handles favourite-longshot
    bias better than plain normalisation, which over-rates longshots."""
    lo, hi = 0.5, 3.0
    for _ in range(iters):
        k = (lo + hi) / 2
        s = sum(p ** k for p in probs)
        if s > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    out = [p ** k for p in probs]
    s = sum(out)
    return [p / s for p in out]


def consensus(event):
    """Return {team_name: (fair_prob, n_books, spread_between_books)} for an
    Odds API event using h2h (moneyline) markets."""
    per_team = {}
    for book in event.get("bookmakers", []):
        mkt = next((m for m in book.get("markets", []) if m.get("key") == "h2h"), None)
        if not mkt:
            continue
        outcomes = mkt.get("outcomes", [])
        if len(outcomes) < 2:
            continue
        # Third-party odds feeds can contain nulls, malformed prices, or
        # transient partial markets. Ignore an invalid book rather than letting
        # one bad outcome crash the entire sports scan or contaminate consensus.
        try:
            prices = [float(o["price"]) for o in outcomes]
            if any(not math.isfinite(p) or p == 0 for p in prices):
                continue
            raw = [american_to_prob(p) if abs(p) >= 100
                   else decimal_to_prob(p) for p in prices]
            if any(not math.isfinite(p) or p <= 0 or p >= 1 for p in raw):
                continue
            fair = devig_power(raw)
            if len(fair) != len(outcomes) or any(
                    not math.isfinite(p) or p <= 0 or p >= 1 for p in fair):
                continue
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            continue
        w = BOOK_WEIGHTS.get(book.get("key"), 1.0)
        for o, p in zip(outcomes, fair):
            per_team.setdefault(o["name"], []).append((p, w))
    out = {}
    for team, vals in per_team.items():
        wsum = sum(w for _, w in vals)
        # blend in log-odds space: averaging probabilities directly is biased
        lo = sum(w * math.log(p / (1 - p)) for p, w in vals) / wsum
        probs = [p for p, _ in vals]
        out[team] = (1 / (1 + math.exp(-lo)), len(vals), max(probs) - min(probs))
    return out


class OddsClient:
    def __init__(self, api_key, timeout=15):
        self.api_key = api_key
        self.timeout = timeout
        self.last_remaining = None

    def fetch(self, sport, regions="us"):
        if not self.api_key:
            return []
        r = requests.get(ODDS_URL.format(sport=sport), params={
            "apiKey": self.api_key, "regions": regions, "markets": "h2h",
            "oddsFormat": "american"}, timeout=self.timeout)
        if r.status_code in (401, 422):
            return []
        r.raise_for_status()
        self.last_remaining = r.headers.get("x-requests-remaining")
        return r.json()


def odds_due(store, sport, refresh_minutes):
    """Respect the free-tier credit budget: only refetch odds when stale."""
    last = store.get_kv(f"odds_last_{sport}", 0)
    return time.time() - last >= refresh_minutes * 60
