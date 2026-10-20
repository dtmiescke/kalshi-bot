"""Schedule spots: rest days, back-to-backs, and long road trips.

Teams on the second night of a back-to-back, or on their 3rd+ straight road
game, underperform measurably (strongest in the NBA and NHL). Sportsbooks
price most of it, so this starts with a small weight and the brain decides
whether it adds anything beyond the line.

Source: ESPN's public scoreboard feed (unofficial, best-effort).
"""
import time
from datetime import datetime, timedelta, timezone

import requests

from .base import Reading, clamp, logit, sigmoid
from .intel import ESPN_PATHS

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
# how much one day of rest disadvantage is worth, in log-odds, by sport
REST_VALUE = {"basketball_nba": 0.12, "icehockey_nhl": 0.10, "baseball_mlb": 0.02,
              "americanfootball_nfl": 0.05, "americanfootball_ncaaf": 0.04}
SCHEDULE_DAYS = 7


def fetch_games(sport, days=SCHEDULE_DAYS, timeout=10, get=requests.get):
    """Recent games as [{date, home, away}], oldest first."""
    path = ESPN_PATHS.get(sport)
    if not path:
        return []
    out = []
    today = datetime.now(timezone.utc).date()
    for d in range(days, -1, -1):
        day = (today - timedelta(days=d)).strftime("%Y%m%d")
        try:
            r = get(SCOREBOARD.format(path=path), params={"dates": day}, timeout=timeout)
            r.raise_for_status()
            for ev in r.json().get("events", []):
                comp = (ev.get("competitions") or [{}])[0]
                teams = {c.get("homeAway"): c.get("team", {}).get("displayName", "")
                         for c in comp.get("competitors", [])}
                if teams.get("home") and teams.get("away"):
                    out.append({"date": ev.get("date", ""), "home": teams["home"],
                                "away": teams["away"]})
        except (requests.RequestException, ValueError):
            continue
    return out


def team_spot(team, games, game_time):
    """Rest days before game_time and current road-game streak."""
    played = sorted((g for g in games if team in (g["home"], g["away"])
                     and g["date"] < game_time), key=lambda g: g["date"])
    if not played:
        return None
    last = datetime.fromisoformat(played[-1]["date"].replace("Z", "+00:00"))
    gt = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
    rest = max(0, (gt.date() - last.date()).days - 1)
    road = 0
    for g in reversed(played):
        if g["away"] == team:
            road += 1
        else:
            break
    return {"rest_days": min(rest, 4), "road_streak": road}


def schedule_spot(team, opp, is_home, game_time, games, base_prob, sport):
    """Shift the consensus by the rest and travel gap between the two teams."""
    if base_prob is None or not games:
        return None
    a, b = team_spot(team, games, game_time), team_spot(opp, games, game_time)
    if not a or not b:
        return None
    value = REST_VALUE.get(sport, 0.05)
    rest_gap = a["rest_days"] - b["rest_days"]
    shift = value * max(-2, min(2, rest_gap))
    # 3rd+ straight road game: extra fatigue for the travelling side
    if not is_home and a["road_streak"] >= 2:
        shift -= value * 0.5
    if is_home and b["road_streak"] >= 2:
        shift += value * 0.5
    if abs(shift) < 1e-6:
        return None
    return Reading("schedule_spot", clamp(sigmoid(logit(base_prob) + shift)),
                   clamp(abs(shift) / (value * 2), 0.1, 0.8),
                   {"team": a, "opponent": b, "shift_logit": round(shift, 4)})


def cached_games(store, sport, fetch=fetch_games, refresh_hours=6):
    key = f"schedule_{sport}"
    cached = store.get_kv(key)
    if cached and time.time() - cached["ts"] < refresh_hours * 3600:
        return cached["games"]
    games = fetch(sport)
    if games:
        store.set_kv(key, {"ts": time.time(), "games": games})
        return games
    return cached["games"] if cached else []
