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
        # Authentication and unsupported-market errors are not valid empty
        # schedules. Raise so callers preserve the last known-good odds cache and
        # emit a source-health warning instead of silently treating every game as
        # unmatched.
        r.raise_for_status()
        self.last_remaining = r.headers.get("x-requests-remaining")
        self.last_used = r.headers.get("x-requests-used")
        return r.json()


def odds_due(store, sport, refresh_minutes):
    """Respect the free-tier credit budget: only refetch odds when stale."""
    last = store.get_kv(f"odds_last_{sport}", 0)
    return time.time() - last >= refresh_minutes * 60


# --- free-tier budget (known problem 8) -----------------------------------------------
# One h2h call costs one credit per region. Plan: every 4 hours, hourly close to
# game time, only leagues with a game soon, pace the month, hard stop near zero.
NEAR_GAME_HOURS = 3
HORIZON_HOURS = 36
RESERVE_CREDITS = 10


def _ts(value):
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def next_game_ts(events, now):
    """Soonest game time among open markets (Kalshi puts it on each market as
    occurrence_datetime; expected_expiration_time is the fallback). Games that
    started over 3 hours ago are ignored."""
    best = None
    for ev in events or []:
        for m in ev.get("markets") or []:
            if m.get("status") not in ("active", "open", None):
                continue
            t = _ts(m.get("occurrence_datetime")) or _ts(m.get("expected_expiration_time"))
            if t and t >= now - 3 * 3600 and (best is None or t < best):
                best = t
    return best


def _month_days_left(now):
    from datetime import datetime, timezone
    import calendar
    d = datetime.fromtimestamp(now, timezone.utc)
    return calendar.monthrange(d.year, d.month)[1] - d.day + 1


def refresh_plan(store, sport, now, next_game, base_minutes, regions="us",
                 monthly_credits=500):
    """(due, reason) for re-pulling one sport's odds within the free budget."""
    cost = max(1, len([r for r in str(regions).split(",") if r.strip()]))
    if next_game is not None and next_game - now > HORIZON_HOURS * 3600:
        return False, "no game soon"
    near = next_game is not None and next_game - now <= NEAR_GAME_HOURS * 3600
    interval = 60 if near else base_minutes
    if now - float(store.get_kv(f"odds_last_{sport}", 0) or 0) < interval * 60:
        return False, "fresh"
    quota = store.get_kv("odds_quota") or {}
    remaining = quota.get("remaining")
    remaining = monthly_credits if remaining is None else float(remaining)
    if remaining - cost < RESERVE_CREDITS:
        return False, "quota exhausted"
    allowance = remaining / _month_days_left(now)
    spent = float(store.get_kv(f"odds_spent_{time.strftime('%Y%m%d', time.gmtime(now))}", 0) or 0)
    # near-game lines are worth the most: they may use up to twice the day's share
    if spent + cost > allowance * (2 if near else 1):
        return False, "daily budget used"
    return True, "near game" if near else "scheduled"


def record_spend(store, client, now, regions="us", monthly_credits=500, alert=None):
    """Book one fetch against today's budget and keep the provider's own counts."""
    cost = max(1, len([r for r in str(regions).split(",") if r.strip()]))
    day = f"odds_spent_{time.strftime('%Y%m%d', time.gmtime(now))}"
    store.set_kv(day, float(store.get_kv(day, 0) or 0) + cost)
    try:
        remaining = float(getattr(client, "last_remaining", None))
    except (TypeError, ValueError):
        return
    used = getattr(client, "last_used", None)
    store.set_kv("odds_quota", {"remaining": remaining, "used": used, "ts": int(now)})
    month = f"odds_quota_alerted_{time.strftime('%Y%m', time.gmtime(now))}"
    if remaining <= 0.2 * monthly_credits and not store.get_kv(month) and alert:
        alert("Odds API credits low", f"{remaining:.0f} of {monthly_credits} free credits left "
              "this month; refreshing only near game time.", tags=["warning"])
        store.set_kv(month, True)
