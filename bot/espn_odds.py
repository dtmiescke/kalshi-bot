"""Free sportsbook moneylines from ESPN's public scoreboard (DraftKings lines, no key, no credit limit).

ESPN attaches a DraftKings line to each game roughly a day before it starts. This turns those lines into the same
event shape as The Odds API so the existing devig / consensus / matching code reads them unchanged. It is the
fallback when The Odds API is out of credits, not yet due, or failing; one book is a weaker signal than six, and the
consensus code already weights it that way.
"""
import math
import time
from datetime import datetime, timedelta, timezone

import requests

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"

# Odds API sport key -> ESPN sport/league path
ESPN_PATHS = {
    "basketball_nba": "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "americanfootball_ncaaf": "football/college-football",
    "baseball_mlb": "baseball/mlb",
    "icehockey_nhl": "hockey/nhl",
    "soccer_epl": "soccer/eng.1",
    "soccer_spain_la_liga": "soccer/esp.1",
    "soccer_germany_bundesliga": "soccer/ger.1",
    "soccer_italy_serie_a": "soccer/ita.1",
    "soccer_france_ligue_one": "soccer/fra.1",
    "soccer_uefa_champs_league": "soccer/uefa.champions",
    "soccer_uefa_europa_league": "soccer/uefa.europa",
}


def _american(value):
    """'-120' / '+300' / 295 -> float, else None."""
    try:
        x = float(str(value).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and abs(x) >= 100 else None


def _price(side):
    side = side if isinstance(side, dict) else {}
    for key in ("close", "open"):          # the current line first; the opener only if there is no current one
        p = _american((side.get(key) or {}).get("odds"))
        if p is not None:
            return p
    return None


def event_from_game(game, three_way=False):
    """One ESPN scoreboard event -> an Odds API shaped event, or None when it has no usable pre-game moneyline."""
    comp = (game.get("competitions") or [{}])[0]
    if ((game.get("status") or {}).get("type") or {}).get("state") != "pre":
        return None                                          # live and finished lines are not tradable consensus
    teams = {c.get("homeAway"): (c.get("team") or {}).get("displayName") for c in comp.get("competitors") or []}
    home, away = teams.get("home"), teams.get("away")
    if not home or not away:
        return None
    for odds in comp.get("odds") or []:
        ml = odds.get("moneyline") or {}
        ph, pa = _price(ml.get("home")), _price(ml.get("away"))
        if ph is None or pa is None:
            continue
        outcomes = [{"name": home, "price": ph}, {"name": away, "price": pa}]
        draw = _price(ml.get("draw"))
        if draw is None and isinstance(odds.get("drawOdds"), dict):
            draw = _american(odds["drawOdds"].get("moneyLine"))
        if draw is not None:
            outcomes.append({"name": "Draw", "price": draw})
        elif three_way:
            continue                                         # a soccer line missing its draw price would mis-state both sides
        return {"id": f"espn:{game.get('id')}", "home_team": home, "away_team": away,
                "commence_time": game.get("date"), "source": "espn",
                "bookmakers": [{"key": "draftkings", "markets": [{"key": "h2h", "outcomes": outcomes}]}]}
    return None


def fetch(sport, days=3, get=None, now=None, timeout=15):
    """Upcoming games with a DraftKings moneyline for one Odds API sport key. Raises on a failed request."""
    path = ESPN_PATHS.get(sport)
    if not path:
        return []
    get = get or requests.get
    start = datetime.fromtimestamp(now or time.time(), timezone.utc)
    seen, events = set(), []
    for offset in range(-1, days):          # ESPN dates are US Eastern, and it rejects a date range, so one call per day
        day = f"{start + timedelta(days=offset):%Y%m%d}"
        r = get(SCOREBOARD.format(path=path), params={"dates": day, "limit": 300}, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict):
            raise RuntimeError("ESPN scoreboard returned an unexpected payload")
        for g in payload.get("events") or []:
            if isinstance(g, dict) and g.get("id") not in seen:
                seen.add(g.get("id"))
                ev = event_from_game(g, sport.startswith("soccer"))
                if ev:
                    events.append(ev)
    return events
