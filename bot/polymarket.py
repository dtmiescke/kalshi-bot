"""Polymarket's public sports moneylines: a second free price with no key, no card and no credit limit.

Polymarket's read-only "gamma" API lists game-winner markets for college football and the pro leagues. Its price is a
prediction-market price (real money, but not a sportsbook), so it is an independent read to set beside DraftKings and
Kalshi, never a thing to trade on. Only reads; a market with thin liquidity or a lopsided/zero price is ignored."""
import json
import logging
import time

import requests

from .matching import name_score

log = logging.getLogger(__name__)

URL = "https://gamma-api.polymarket.com/events"
TAGS = {"cfb": "football/college-football", "nfl": "football/nfl", "nba": "basketball/nba", "nhl": "hockey/nhl",
        "mlb": "baseball/mlb"}
MIN_LIQUIDITY = 1000.0          # dollars resting in the market; thinner prices are noise
MIN_NAME = 0.6


def _list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def games_from_events(events):
    """[{"teams": {name: chance}, "end": iso}] for each event with a liquid two-way moneyline, chances summing to ~1."""
    out = []
    for ev in events or []:
        for m in ev.get("markets") or []:
            if m.get("sportsMarketType") != "moneyline":
                continue
            names, prices = _list(m.get("outcomes")), _list(m.get("outcomePrices"))
            try:
                prices = [float(p) for p in prices]
                liq = float(m.get("liquidity") or 0)
            except (TypeError, ValueError):
                continue
            if len(names) != 2 or len(prices) != 2 or liq < MIN_LIQUIDITY or not 0.9 <= sum(prices) <= 1.1:
                continue
            if any(p <= 0.01 or p >= 0.99 for p in prices):
                continue                       # settled or one-sided: not a live price
            tot = sum(prices)
            out.append({"teams": {n: p / tot for n, p in zip(names, prices)}, "end": ev.get("endDate")})
    return out


def fetch(tag="cfb", get=requests.get, limit=100):
    """Open events for one league tag. Raises on a failed request."""
    r = get(URL, params={"closed": "false", "tag_slug": tag, "limit": limit, "order": "volume24hr", "ascending": "false"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Polymarket returned an unexpected payload")
    return games_from_events(data)


def chance(games, team, opponent=None):
    """Polymarket's chance that `team` wins, from the game whose two names best match, or None."""
    best = None
    for g in games:
        names = list(g["teams"])
        for n in names:
            s = name_score(team, n)
            if opponent is not None:
                other = next((x for x in names if x != n), None)
                s = min(s, name_score(opponent, other)) if other else 0
            if s >= MIN_NAME and (best is None or s > best[0]):
                best = (s, g["teams"][n])
    return best[1] if best else None


def cached(store, tag="cfb", every=1800, now=None, get=requests.get):
    """The league's games, fetched at most every `every` seconds and kept in kv. Never raises; [] when unavailable."""
    now = int(time.time() if now is None else now)
    key = f"polymarket_{tag}"
    try:
        hit = store.get_kv(key)
        if isinstance(hit, dict) and now - hit.get("ts", 0) < every:
            return hit["games"]
        games = fetch(tag, get)
        store.set_kv(key, {"ts": now, "games": games})
        return games
    except Exception:
        log.warning("polymarket read failed", exc_info=True)
        hit = store.get_kv(key) if store is not None else None
        return hit["games"] if isinstance(hit, dict) else []
