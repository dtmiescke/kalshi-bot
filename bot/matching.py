"""Link a Kalshi game market to the same game in the sportsbook feed.

Kalshi labels teams loosely ("Boston", "BOS", "Celtics"); books use full names
("Boston Celtics"). We match on name similarity AND require the start times to
be close, so two games involving the same city can't get crossed.
"""
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

ALIASES = {
    # common short forms that fuzzy matching misses; extend freely
    "ny": "new york", "la": "los angeles", "sf": "san francisco",
    "kc": "kansas city", "tb": "tampa bay", "gb": "green bay",
    "no": "new orleans", "ne": "new england", "lv": "las vegas",
}


def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    words = [ALIASES.get(w, w) for w in s.split()]
    return " ".join(words)


def name_score(kalshi_name, book_name):
    a, b = norm(kalshi_name), norm(book_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # "boston" inside "boston celtics", or "celtics" inside it
    if a in b or b in a:
        return 0.9
    a_words, b_words = set(a.split()), set(b.split())
    overlap = len(a_words & b_words) / max(1, min(len(a_words), len(b_words)))
    return max(overlap * 0.85, SequenceMatcher(None, a, b).ratio())


def _parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def match_team(kalshi_team, kalshi_time, odds_events, max_hours=36, min_score=0.6):
    """Return (event, book_team_name, score) for the best match, or None."""
    kt = _parse_time(kalshi_time)
    best = None
    for ev in odds_events:
        et = _parse_time(ev.get("commence_time"))
        if kt and et and abs((kt - et).total_seconds()) > max_hours * 3600:
            continue
        for team in (ev.get("home_team"), ev.get("away_team")):
            sc = name_score(kalshi_team, team)
            if sc >= min_score and (best is None or sc > best[2]):
                best = (ev, team, sc)
    return best


def match_event(kalshi_teams, kalshi_time, odds_events, max_hours=96, min_score=0.6):
    """Match a whole Kalshi game (both teams) to one sportsbook event.

    Requiring BOTH teams to match is far safer than matching one name: it stops
    "Los Angeles" from pairing with the wrong LA team. Returns
    ({kalshi_team: book_team}, event) or (None, None).
    """
    if len(kalshi_teams) != 2:
        return None, None
    kt = _parse_time(kalshi_time)
    best, best_score = None, 0.0
    a, b = kalshi_teams
    for ev in odds_events:
        et = _parse_time(ev.get("commence_time"))
        if kt and et and abs((kt - et).total_seconds()) > max_hours * 3600:
            continue
        h, w = ev.get("home_team"), ev.get("away_team")
        for x, y in ((h, w), (w, h)):
            sa, sb = name_score(a, x), name_score(b, y)
            if min(sa, sb) >= min_score and sa + sb > best_score:
                best_score, best = sa + sb, ({a: x, b: y}, ev)
    return best if best else (None, None)
