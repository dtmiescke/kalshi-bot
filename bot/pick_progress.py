"""How far each open shadow pick's player is toward the line, from ESPN's free live box scores.

For a pick like "Jayson Tatum 25+ points (NO)" this reads where the game is and what he has so far (12 points, 18 minutes,
Q3 4:12), so the app can show "12 of 25" and what pace he is on. It only calls ESPN while a pick's game is in progress or has
just finished; read-only, and it never touches a bet."""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from . import nba_tonight as nba
from .matching import name_score, norm

log = logging.getLogger(__name__)

SPORTS = {"NBA": "basketball/nba", "NFL": "football/nfl"}
SITE = "https://site.api.espn.com/apis/site/v2/sports/{path}"
# pick label stat -> list of (box-score category or None, column label) to add up, and a short unit
NBA_STATS = {"points": [("PTS",)], "rebounds": [("REB",)], "assists": [("AST",)], "steals": [("STL",)], "blocks": [("BLK",)],
             "threes": [("3PT", "made")], "pts+reb+ast": [("PTS",), ("REB",), ("AST",)], "pts+reb": [("PTS",), ("REB",)],
             "pts+ast": [("PTS",), ("AST",)], "reb+ast": [("REB",), ("AST",)], "steals+blocks": [("STL",), ("BLK",)]}
NFL_STATS = {"passing yards": ("passing", "YDS"), "passing TDs": ("passing", "TD"), "receiving yards": ("receiving", "YDS"),
             "receptions": ("receiving", "REC"), "rushing yards": ("rushing", "YDS"), "rushing attempts": ("rushing", "CAR")}
UNIT = {"points": "pts", "rebounds": "reb", "assists": "ast", "steals": "stl", "blocks": "blk", "threes": "threes"}
_LABEL = re.compile(r"^(.*?) (\d+)\+ (.+?) \((YES|NO)\)$")
GAME_MINUTES = {"NBA": 48.0}


def parse_label(label):
    """(player, line, stat, side) from 'Jayson Tatum 25+ points (NO)', or None for a game winner."""
    m = _LABEL.match(label or "")
    return (m.group(1).strip(), int(m.group(2)), m.group(3), m.group(4).lower()) if m else None


def _num(v):
    try:
        return float(str(v).split("-")[0])        # "3-7" (made-attempted) reads as 3
    except (TypeError, ValueError):
        return None


def nba_value(athlete_stats, labels, stat):
    """Current value of a stat from one NBA box-score row (labels line up with athlete_stats)."""
    row = dict(zip(labels, athlete_stats))
    parts = NBA_STATS.get(stat)
    if not parts:
        return None
    total = 0.0
    for part in parts:
        v = _num(row.get(part[0]))
        if v is None:
            return None
        total += v
    return total


def read_box(sport, summary, player, stat):
    """(value, minutes) for a player in an ESPN summary payload, or None if he has no line yet."""
    want = norm(player)
    for team in (summary.get("boxscore") or {}).get("players") or []:
        for cat in team.get("statistics") or []:
            labels = cat.get("labels") or []
            for a in cat.get("athletes") or []:
                name = (a.get("athlete") or {}).get("displayName") or ""
                if norm(name) != want and name_score(player, name) < 0.9:
                    continue
                stats = a.get("stats") or []
                if sport == "NBA":
                    if not stats:
                        return None                  # on the roster but has not played
                    row = dict(zip(labels, stats))
                    return nba_value(stats, labels, stat), _num(row.get("MIN"))
                spec = NFL_STATS.get(stat)
                if spec and cat.get("name") == spec[0] and spec[1] in labels:
                    return _num(stats[labels.index(spec[1])]), None
    return None


def game_status(event):
    comp = (event.get("competitions") or [{}])[0]
    t = (event.get("status") or {}).get("type") or {}
    teams = {c.get("homeAway"): ((c.get("team") or {}).get("displayName"), (c.get("team") or {}).get("abbreviation"), c.get("score"))
             for c in comp.get("competitors") or []}
    score = None
    if "home" in teams and "away" in teams:
        score = f'{teams["away"][1]} {teams["away"][2]} - {teams["home"][1]} {teams["home"][2]}'
    return {"state": t.get("state"), "clock": t.get("shortDetail") or t.get("detail"), "score": score,
            "home": (teams.get("home") or (None,))[0], "away": (teams.get("away") or (None,))[0]}


def find_event(events, game):
    """The scoreboard event for 'Away at Home' names."""
    parts = [p.strip() for p in str(game).split(" at ")]
    if len(parts) != 2:
        return None
    for ev in events:
        s = game_status(ev)
        if s["home"] and s["away"] and min(name_score(parts[0], s["away"]), name_score(parts[1], s["home"])) >= 0.8:
            return ev
    return None


def _get(url, params=None, fetch=requests.get):
    return nba._get(url, params, fetch)


def refresh(store, now=None, every=120, fetch=requests.get):
    """Update the progress of open picks whose game has started. Returns how many picks were read, or None."""
    now = int(time.time() if now is None else now)
    try:
        if now - float(store.get_kv("pick_progress_last", 0) or 0) < every:
            return None
        picks = []
        for sport, table in (("NBA", "nba_picks"), ("NFL", "nfl_picks")):
            try:
                picks += [(sport, r) for r in store.q(f"SELECT ticker, game, label, side, tip_ts FROM {table} WHERE status='open' AND COALESCE(tip_ts, 0) <= ?", (now,))]
            except Exception:
                continue
        store.set_kv("pick_progress_last", now)
        if not picks:
            store.set_kv("pick_progress", {})
            return 0
        out, boards, boxes = {}, {}, {}
        days = {(datetime.fromtimestamp(now, timezone.utc) + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0)}
        for sport, r in picks:
            parsed = parse_label(r["label"])
            if not parsed:
                continue
            player, line, stat, _ = parsed
            if sport not in boards:
                evs = []
                for d in sorted(days):
                    evs += _get(SITE.format(path=SPORTS[sport]) + "/scoreboard", {"dates": d}, fetch).get("events") or []
                boards[sport] = evs
            ev = find_event(boards[sport], r["game"])
            if not ev:
                continue
            status = game_status(ev)
            if status["state"] == "pre":
                continue
            key = (sport, ev["id"])
            if key not in boxes:
                boxes[key] = _get(SITE.format(path=SPORTS[sport]) + "/summary", {"event": ev["id"]}, fetch)
            got = read_box(sport, boxes[key], player, stat)
            entry = {"line": line, "stat": stat, "unit": UNIT.get(stat, stat), "state": status["state"], "clock": status["clock"],
                     "score": status["score"], "ts": now, "cur": None, "min": None}
            if got:
                entry["cur"], entry["min"] = got
            out[r["ticker"]] = entry
        store.set_kv("pick_progress", out)
        return len(out)
    except Exception:
        log.warning("pick progress failed", exc_info=True)
        return None
