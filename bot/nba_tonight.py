"""NBA tonight: player props and the moneyline, projected from last season, this season and recent form.

For each game on tonight's slate it reads, from ESPN's free public data, both teams' standings and scoring
(this season and last) and every Kalshi-listed player's game logs (this season and last). Then, per player:

  projection = 0.6 x (this season blended with last season, weighted by games played) + 0.4 x (his last 10 games)

so a player's production and his recent momentum both count, and early in the season (a handful of games) last
season carries most of the weight. His real game-to-game spread (the shape of his last up to 50 games, rescaled to
the projection) turns that into the chance of clearing each Kalshi line ("Okongwu 15+ points"). Each rung is then
compared with Kalshi's real bid and ask, after the taker fee.

The moneyline uses each team's points scored minus allowed per game (this season blended with last), home court
worth 2.5 points, and a 12-point game-to-game spread.

Picks are recorded in a SHADOW account (its own table, fixed $5 stakes, settled from Kalshi's real results), never
through the engine, so they are graded honestly and cannot touch the real ledger. This is research, not advice:
Kalshi's prices track sharp sportsbooks closely, and nothing here is proven to beat them until the shadow results say so."""
import re
import time
from datetime import datetime
from statistics import NormalDist
from zoneinfo import ZoneInfo

import requests

from . import momentum, risk
from .matching import name_score, norm

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_V2 = "https://site.api.espn.com/apis/v2/sports/basketball/nba"
ESPN_LOG = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/gamelog"
CURRENT, LAST = 2027, 2026                  # ESPN labels a season by the year it ends in

# Kalshi player-prop series -> the box-score columns they add up
SERIES = {"KXNBAPTS": ("PTS",), "KXNBAREB": ("REB",), "KXNBAAST": ("AST",), "KXNBA3PT": ("3PM",),
          "KXNBASTL": ("STL",), "KXNBABLK": ("BLK",), "KXNBAPRA": ("PTS", "REB", "AST"), "KXNBAPR": ("PTS", "REB"),
          "KXNBAPA": ("PTS", "AST"), "KXNBARA": ("REB", "AST"), "KXNBASTOCK": ("STL", "BLK")}
LABEL = {"KXNBAPTS": "points", "KXNBAREB": "rebounds", "KXNBAAST": "assists", "KXNBA3PT": "threes", "KXNBASTL": "steals",
         "KXNBABLK": "blocks", "KXNBAPRA": "pts+reb+ast", "KXNBAPR": "pts+reb", "KXNBAPA": "pts+ast", "KXNBARA": "reb+ast",
         "KXNBASTOCK": "stl+blk"}
PRIOR_GAMES = 12            # last season counts as this many games of evidence for a player
TEAM_PRIOR = 20             # ... and this many for a team
HOME_EDGE, GAME_SD = 2.5, 12.0
MIN_EDGE, ML_EDGE, MAX_SPREAD, STAKE = 0.08, 0.03, 0.08, 5.0
MAX_PER_GAME = 8            # picks recorded for one game, across every hourly pass (they used to pile up to 20)
MAX_PER_PLAYER = 2          # ... and for one player: his lines all win or lose together
MAX_PER_STAT = 2            # ... and for one kind of stat in a game, so a night is not eight "points" bets
MAX_SAME_SIDE = 5           # ... and how many may lean the same way (all unders, all overs) out of the 8
BOOK_AGREE = 0.12           # a game-winner pick must be within this of the sportsbook consensus
SHRINK = 0.35               # a disagreement with Kalshi is trusted this much for the cautious check (the bot's own learned trust is lower)
MIN_HISTORY = 15            # games needed before a prop is rated
_ET = ZoneInfo("America/New_York")
SCHEMA = """CREATE TABLE IF NOT EXISTS nba_picks (
    ticker TEXT PRIMARY KEY, ts INTEGER, game TEXT, kind TEXT, label TEXT, side TEXT, price REAL, contracts INTEGER,
    stake REAL, fee REAL, p_model REAL, edge REAL, tip_ts INTEGER, status TEXT, result TEXT, pnl REAL, settled_ts INTEGER)"""


# ---------- pure maths ----------------------------------------------------------------------------------------

def parse_gamelog(payload):
    """ESPN game-log JSON -> [{"date", "season_type", "MIN", "PTS", "REB", "AST", "STL", "BLK", "3PM", "FTM"}] oldest
    first. Preseason games are left out (their minutes are not real)."""
    labels = payload.get("labels") or []
    dates = {k: v.get("gameDate") for k, v in (payload.get("events") or {}).items()}
    out = []
    for st in payload.get("seasonTypes") or []:
        name = str(st.get("displayName") or "")
        if "Preseason" in name:
            continue
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                row = dict(zip(labels, ev.get("stats") or []))
                try:
                    g = {"date": dates.get(ev.get("eventId")) or "", "season_type": name,
                         "MIN": float(row.get("MIN") or 0)}
                    for k in ("PTS", "REB", "AST", "STL", "BLK"):
                        g[k] = float(row.get(k) or 0)
                    g["3PM"] = float(str(row.get("3PT") or "0-0").split("-")[0])
                    g["FTM"] = float(str(row.get("FT") or "0-0").split("-")[0])
                except ValueError:
                    continue
                out.append(g)
    return sorted(out, key=lambda g: g["date"])


def value(game, comps):
    return sum(game.get(c, 0.0) for c in comps)


def _rate(gs, comps):
    minutes = sum(g["MIN"] for g in gs)
    return sum(value(g, comps) for g in gs) / minutes if minutes else None




def projection(games, comps, cur_start="2026-10-01"):
    """(projected mean, details). Production per MINUTE (this season blended with last by games played, 60%) plus his
    last 10 games (40%), multiplied by the minutes he is actually playing now (the last 5 games count most). Working
    in minutes is what lets a bigger role, or a returning injury, show up straight away. Minutes are never projected
    above what he normally plays (his season average), however many he has played lately or whoever is out."""
    played = [g for g in games if g["MIN"] >= 8]
    if len(played) < 3:
        return None, {}
    cur = [g for g in played if g["date"][:10] >= cur_start]
    prior = [g for g in played if g["date"][:10] < cur_start]
    r_cur = _rate(cur, comps)
    r_prior = _rate(prior, comps) if prior else r_cur
    n = len(cur)
    base = r_prior if not cur else (n * r_cur + PRIOR_GAMES * r_prior) / (n + PRIOR_GAMES)
    r10 = _rate(played[-10:], comps)
    exp_min = 0.7 * (sum(g["MIN"] for g in played[-5:]) / len(played[-5:])) + 0.3 * (sum(g["MIN"] for g in played[-15:]) / len(played[-15:]))
    typical = cur if len(cur) >= 5 else played[-30:]
    exp_min = min(exp_min, sum(g["MIN"] for g in typical) / len(typical))
    mu = (0.6 * base + 0.4 * r10) * exp_min
    per_game = lambda gs: round(sum(value(g, comps) for g in gs) / len(gs), 1) if gs else None
    return mu, {"season": per_game(cur), "last_season": per_game(prior), "l10": per_game(played[-10:]),
                "l5": per_game(played[-5:]), "minutes": round(exp_min, 1), "games_this_season": n, "history": len(played)}


def prob_at_least(games, comps, mu, exp_min, k):
    """Chance of a stat >= k: his real game-to-game spread (his last up to 50 games, restated at the minutes he is
    expected to play) rescaled so its average is the projection."""
    hist = [value(g, comps) * exp_min / g["MIN"] for g in games[-50:] if g["MIN"] >= 12]
    if len(hist) < 8:
        return None
    avg = sum(hist) / len(hist)
    if avg <= 0:
        return None
    scale = mu / avg
    hit = sum(1 for x in hist if x * scale >= k - 0.5)
    return (hit + 0.5) / (len(hist) + 1)


def side_edges(p, bid, ask, stake=STAKE, fee_rate=0.07):
    """Both sides' edge after the real fee at the real quote. Returns a list of dicts, best first."""
    out = []
    for side, win, cost in (("yes", p, ask), ("no", 1 - p, None if bid is None else 1 - bid)):
        if cost is None or not 0.04 <= cost <= 0.96:
            continue
        contracts = max(1, int(stake // cost))
        fee = risk.taker_fee(contracts, cost, fee_rate)
        out.append({"side": side, "price": round(cost, 4), "contracts": contracts, "fee": fee,
                    "p_win": round(win, 4), "edge": round(win - cost - fee / contracts, 4)})
    return sorted(out, key=lambda r: -r["edge"])


def team_strength(cur, last):
    """Points scored minus allowed per game: this season blended with last by games played."""
    gc = cur.get("games", 0)
    nc, nl = cur.get("net"), last.get("net")
    if nc is None and nl is None:
        return None
    if nl is None:
        return nc
    if nc is None or gc == 0:
        return nl
    return (gc * nc + TEAM_PRIOR * nl) / (gc + TEAM_PRIOR)


MATCHUP_COMPS = {"PTS", "3PM"}       # scoring is the only production a weak or strong defence plainly moves
MATCHUP_MAX = 0.05                   # the matchup never moves a projection by more than 5%
BACK_TO_BACK = 0.97                  # second night in a row: a small, well-known drag on production


def _allowed(cur, last, tid):
    """Points a team allows per game: this season blended with last by games played."""
    c, l = cur.get(tid) or {}, last.get(tid) or {}
    oc, ol = c.get("oppg"), l.get("oppg")
    if oc is None:
        return ol
    if ol is None or not c.get("games"):
        return oc
    return (c["games"] * oc + TEAM_PRIOR * ol) / (c["games"] + TEAM_PRIOR)


def matchup_factor(comps, opp_id, cur, last):
    """Scoring multiplier from tonight's defence: weaker than the league average lifts it, tougher trims it (capped)."""
    if not MATCHUP_COMPS & set(comps):
        return 1.0
    allowed = [a for a in (_allowed(cur, last, t) for t in set(cur) | set(last)) if a]
    mine = _allowed(cur, last, opp_id)
    if not allowed or not mine:
        return 1.0
    league = sum(allowed) / len(allowed)
    return 1 + max(-MATCHUP_MAX, min(MATCHUP_MAX, 0.5 * (mine - league) / league))


def rest_factor(games, tip_iso):
    """BACK_TO_BACK if he played the calendar day (ET) before tip-off, else 1.0."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    try:
        tip = datetime.fromisoformat(str(tip_iso).replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date()
    except (TypeError, ValueError):
        return 1.0
    last = next((g["date"][:10] for g in reversed(games) if g["MIN"] >= 8), None)
    return BACK_TO_BACK if last and last == (tip - timedelta(days=1)).isoformat() else 1.0


def home_win_prob(s_home, s_away):
    return NormalDist().cdf((s_home - s_away + HOME_EDGE) / GAME_SD)


# ---------- data ----------------------------------------------------------------------------------------------

def _get(url, params=None, fetch=requests.get, retries=3):
    for i in range(retries):
        try:
            r = fetch(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.5 * (i + 1))
    return {}


def standings(season, fetch=requests.get):
    """{espn_team_id: {"net", "games", "ppg", "oppg", "record"}} for a season."""
    data = _get(ESPN_V2 + "/standings", {"season": season}, fetch)
    out = {}
    for child in data.get("children") or []:
        for e in (child.get("standings") or {}).get("entries") or []:
            s = {x["name"]: x.get("value") for x in e.get("stats") or []}
            try:
                w, l = int(s.get("wins") or 0), int(s.get("losses") or 0)
                pf, pa = float(s.get("avgPointsFor")), float(s.get("avgPointsAgainst"))
            except (TypeError, ValueError):
                continue
            out[str(e["team"]["id"])] = {"net": pf - pa, "games": w + l, "ppg": pf, "oppg": pa, "record": f"{w}-{l}",
                                         "name": e["team"].get("displayName")}
    return out


def tonight_games(day=None, fetch=requests.get):
    """ESPN scoreboard for an ET date (YYYYMMDD) -> [{"id", "tip", "home", "away"}], each team {"id","abbr","name"}."""
    data = _get(ESPN + "/scoreboard", {"dates": day} if day else None, fetch)
    games = []
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c["homeAway"]: {"id": str(c["team"]["id"]), "abbr": c["team"]["abbreviation"],
                                 "name": c["team"]["displayName"]} for c in comp.get("competitors") or []}
        if "home" in teams and "away" in teams:
            games.append({"id": ev["id"], "tip": ev.get("date"), "status": ((ev.get("status") or {}).get("type") or {}).get("name"),
                          **teams})
    return games


def roster(team_id, fetch=requests.get):
    data = _get(f"{ESPN}/teams/{team_id}/roster", None, fetch)
    return [{"id": a["id"], "name": a.get("displayName") or a.get("fullName"),
             "injuries": [i.get("status") for i in a.get("injuries") or [] if isinstance(i, dict)]}
            for a in data.get("athletes") or [] if isinstance(a, dict) and a.get("id")]


def injuries_feed(fetch=requests.get, base=ESPN):
    """ESPN's league-wide injury list -> {team display name: [{"name", "status", "comment", "level"}]}. level is
    "out" (out or doubtful), "doubt" (day-to-day or questionable: his minutes are uncertain) or "ok"."""
    data = _get(base + "/injuries", None, fetch)
    out = {}
    for t in data.get("injuries") or []:
        rows = []
        for i in t.get("injuries") or []:
            a = i.get("athlete") or {}
            text = (i.get("shortComment") or i.get("longComment") or "")
            low = (text + " " + str(i.get("status") or "")).lower()
            if str(i.get("status") or "").lower() == "out" or "doubtful" in low or "ruled out" in low or "injured reserve" in low:
                level = "out"
            elif "day-to-day" in low or "questionable" in low or "game-time" in low or "minutes" in low:
                level = "doubt"
            else:
                level = "ok"
            rows.append({"name": a.get("displayName"), "status": i.get("status"), "comment": text[:160], "level": level})
        out[t.get("displayName")] = rows
    return out


def net_loss(pts, ast):
    """Points of team strength lost when a player with these per-game numbers is out (a heuristic: about 0.12 points
    of margin per point or 1.5-assist-equivalent he produces, so a 28-point, 7-assist star is worth about 4.6)."""
    return 0.12 * (pts + 1.5 * ast)


def player_games(athlete_id, fetch=requests.get):
    games = []
    for season in (LAST, CURRENT):
        games += parse_gamelog(_get(ESPN_LOG.format(id=athlete_id), {"season": season}, fetch))
        time.sleep(0.15)
    return sorted(games, key=lambda g: g["date"])


# ---------- Kalshi ----------------------------------------------------------------------------------------------

_PLAYER = re.compile(r"^(.*?):\s*(\d+)\+")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def kalshi_events(client, series, now=None):
    """Open events (with markets) for a Kalshi series."""
    return client._paged("/events", "events", {"series_ticker": series, "status": "open",
                                               "with_nested_markets": "true", "limit": 200}, max_pages=2)


def event_game(ev, games):
    """Which ESPN game a Kalshi event is: both team names in its title must match the game's two teams."""
    title = str(ev.get("title") or "").split(":")[0]
    parts = [p.strip() for p in re.split(r"\s+(?:vs\.?|at|@)\s+", title)]
    if len(parts) != 2:
        return None
    for g in games:
        for a, b in ((g["home"]["name"], g["away"]["name"]), (g["away"]["name"], g["home"]["name"])):
            if min(name_score(parts[0], a), name_score(parts[1], b)) >= 0.6:
                return g
    return None


def find_player(name, roster_rows):
    want = norm(name)
    best = None
    for r in roster_rows:
        s = 1.0 if norm(r["name"]) == want else name_score(name, r["name"])
        if s >= 0.85 and (best is None or s > best[0]):
            best = (s, r)
    return best[1] if best else None


# ---------- the report ----------------------------------------------------------------------------------------

def _cached(store, key, ttl, make, now):
    """Small kv cache so ESPN is not hit again for the same player or team within ttl seconds."""
    if store is not None:
        hit = store.get_kv(key)
        if isinstance(hit, dict) and now - hit.get("ts", 0) < ttl:
            return hit["data"]
    data = make()
    if store is not None and data:
        store.set_kv(key, {"ts": now, "data": data})
    return data


def book_prob(store, team_name, now):
    """Latest devigged sportsbook probability for a team tonight, from the odds the bot already collects."""
    if store is None:
        return None
    try:
        rows = store.q("""SELECT team, fair_prob FROM odds_snapshots WHERE sport='basketball_nba' AND ts >= ?
                          ORDER BY ts DESC""", (int(now) - 8 * 3600,))
    except Exception:
        return None
    best = None
    for r in rows:
        sc = name_score(team_name, r["team"])
        if sc >= 0.8 and (best is None or sc > best[0]):
            best = (sc, r["fair_prob"])
    return best[1] if best else None


UNIT_NBA = {"KXNBAPTS": "pts", "KXNBAREB": "reb", "KXNBAAST": "ast", "KXNBA3PT": "threes", "KXNBASTL": "stl", "KXNBABLK": "blk",
            "KXNBAPRA": "pts+reb+ast", "KXNBAPR": "pts+reb", "KXNBAPA": "pts+ast", "KXNBARA": "reb+ast", "KXNBASTOCK": "stl+blk"}
_LABEL_PARTS = re.compile(r"^(.*?) \d+\+ (.+?) \((YES|NO)\)$")


def label_parts(label, kind="prop"):
    """(player, stat, side) from a pick label; a game winner has no player or stat."""
    m = _LABEL_PARTS.match(label or "")
    return (m.group(1), m.group(2), m.group(3).lower()) if m and kind == "prop" else (None, None, None)


def allowed(counts, game, player, stat, side, limits=None):
    """May one more pick join a game that already holds `counts`? limits: (per game, per player, per stat, same side)."""
    per_game, per_player, per_stat, same_side = limits or (MAX_PER_GAME, MAX_PER_PLAYER, MAX_PER_STAT, MAX_SAME_SIDE)
    if counts.get(("game", game), 0) >= per_game:
        return False
    if player and counts.get(("player", game, player), 0) >= per_player:
        return False
    if stat and counts.get(("stat", game, stat), 0) >= per_stat:
        return False
    return not (side and counts.get(("side", game, side), 0) >= same_side)


def tally(counts, game, player, stat, side):
    for key in (("game", game), ("player", game, player) if player else None, ("stat", game, stat) if stat else None,
                ("side", game, side) if side else None):
        if key:
            counts[key] = counts.get(key, 0) + 1


def build_report(client, store=None, now=None, day=None, fetch=requests.get, max_props_per_game=6):
    """Everything for tonight's slate. Returns {"games": [...], "picks": [...], "ts": now}. Never trades."""
    now = int(time.time() if now is None else now)
    day = day or datetime.fromtimestamp(now, _ET).strftime("%Y%m%d")
    games = [g for g in tonight_games(day, fetch) if g.get("status") in (None, "STATUS_SCHEDULED")]
    cur = _cached(store, "nba_standings_cur", 6 * 3600, lambda: standings(CURRENT, fetch), now)
    last = _cached(store, "nba_standings_last", 7 * 86400, lambda: standings(LAST, fetch), now)
    cur_start = "2026-10-01"
    report = {"ts": now, "day": day, "games": [], "picks": []}
    rosters, gl = {}, {}
    for g in games:
        for t in (g["home"], g["away"]):
            rosters[t["id"]] = _cached(store, f"nba_roster_{t['id']}", 12 * 3600, lambda t=t: roster(t["id"], fetch), now)

    def logs(pl):
        if pl["id"] not in gl:
            gl[pl["id"]] = _cached(store, f"nba_games_{pl['id']}", 6 * 3600, lambda: player_games(pl["id"], fetch), now)
        return gl[pl["id"]]

    # lineup news: who is out tonight, what that costs the team, and where their minutes go
    inj = _cached(store, "nba_injuries", 1800, lambda: injuries_feed(fetch), now)
    prop_events = {sr: [ev for ev in kalshi_events(client, sr) if event_game(ev, games)] for sr in SERIES}
    listed = {}                                      # team id -> {player id: player}: the rotation Kalshi prices tonight
    for sr, evs in prop_events.items():
        for ev in evs:
            g = event_game(ev, games)
            ros = rosters[g["home"]["id"]] + rosters[g["away"]["id"]]
            for m in ev.get("markets") or []:
                mt = _PLAYER.match(m.get("yes_sub_title") or "")
                pl = find_player(mt.group(1).strip(), ros) if mt else None
                if pl:
                    team = g["home"]["id"] if any(r["id"] == pl["id"] for r in rosters[g["home"]["id"]]) else g["away"]["id"]
                    listed.setdefault(team, {})[pl["id"]] = pl
    team_info = {}                                   # team id -> {"absent": [...], "doubt": [...], "net": float, "extra": float}
    for g in games:
        for t in (g["home"], g["away"]):
            rows, absent, doubt = inj.get(t["name"], []), [], []
            for r in rows:
                pl = find_player(r["name"] or "", rosters[t["id"]])
                if not pl or r["level"] == "ok":
                    continue
                played = [x for x in logs(pl) if x["MIN"] >= 8]
                if r["level"] == "doubt":
                    doubt.append({"name": pl["name"], "status": r["status"], "comment": r["comment"]})
                    continue
                if len(played) < 3:
                    absent.append({"name": pl["name"], "status": r["status"], "comment": r["comment"], "minutes": 0.0, "pts": 0.0, "ast": 0.0, "id": pl["id"]})
                    continue
                last10 = played[-10:]
                absent.append({"name": pl["name"], "status": r["status"], "comment": r["comment"], "id": pl["id"],
                               "minutes": round(sum(x["MIN"] for x in played[-5:]) / len(played[-5:]), 1),
                               "pts": round(sum(x["PTS"] for x in last10) / len(last10), 1),
                               "ast": round(sum(x["AST"] for x in last10) / len(last10), 1)})
            out_ids = {a["id"] for a in absent}
            doubt_names = {norm(d["name"]) for d in doubt}
            active = [pl for pid, pl in listed.get(t["id"], {}).items() if pid not in out_ids and norm(pl["name"]) not in doubt_names]
            mins = {pl["id"]: (lambda pg: sum(x["MIN"] for x in pg[-5:]) / len(pg[-5:]) if pg else 0.0)([x for x in logs(pl) if x["MIN"] >= 8]) for pl in active}
            team_info[t["id"]] = {"absent": absent, "doubt": doubt, "net": -min(12.0, sum(net_loss(a["pts"], a["ast"]) for a in absent)),
                                  "out_min": sum(a["minutes"] for a in absent), "active_min": sum(mins.values()), "mins": mins}

    # moneyline
    ml_markets = {}
    for ev in kalshi_events(client, "KXNBAGAME"):
        g = event_game(ev, games)
        if g:
            ml_markets[g["id"]] = ev
    for g in games:
        h, a = g["home"], g["away"]
        sh = team_strength(cur.get(h["id"], {}), last.get(h["id"], {}))
        sa = team_strength(cur.get(a["id"], {}), last.get(a["id"], {}))
        if sh is not None:
            sh += team_info[h["id"]]["net"]                   # minus what the players who are out are worth
        if sa is not None:
            sa += team_info[a["id"]]["net"]
        entry = {"id": g["id"], "tip": g["tip"], "home": h["name"], "away": a["name"], "ml": None, "props": [],
                 "lineups": {k: {"absent": [{x: y for x, y in r.items() if x != "id"} for r in team_info[t["id"]]["absent"]],
                                 "doubtful": team_info[t["id"]]["doubt"], "net_adjustment": round(team_info[t["id"]]["net"], 1)}
                             for k, t in (("home", h), ("away", a))}}
        for side_key in ("home", "away"):
            t = g[side_key]
            entry[side_key + "_stats"] = {"record": cur.get(t["id"], {}).get("record"), "ppg": round(cur.get(t["id"], {}).get("ppg", 0), 1) or None,
                                          "oppg": round(cur.get(t["id"], {}).get("oppg", 0), 1) or None,
                                          "last_ppg": round(last.get(t["id"], {}).get("ppg", 0), 1) or None,
                                          "last_record": last.get(t["id"], {}).get("record")}
        if sh is not None and sa is not None and g["id"] in ml_markets:
            p_home = home_win_prob(sh, sa)
            quotes = {}
            for m in ml_markets[g["id"]].get("markets", []):
                label = m.get("yes_sub_title") or ""
                team = h if name_score(label, h["name"]) >= name_score(label, a["name"]) else a
                quotes[team["id"]] = (m["ticker"], _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars")), m)
            opts = []
            for team, p in ((h, p_home), (a, 1 - p_home)):
                if team["id"] not in quotes:
                    continue
                tk, bid, ask, m = quotes[team["id"]]
                if ask is None or bid is None:
                    continue
                book = book_prob(store, team["name"], now)
                mid = (bid + ask) / 2
                p_cal = mid + SHRINK * (p - mid)
                cal = {e["side"]: e["edge"] for e in side_edges(p_cal, bid, ask)}
                for e in side_edges(p, bid, ask):
                    if e["side"] == "yes":
                        opts.append({"ticker": tk, "team": team["name"], "p_model": round(p, 3), "p_book": None if book is None else round(book, 3),
                                     "bid": bid, "ask": ask, "cautious_edge": cal.get("yes"), **e})
            entry["ml"] = {"model_home": round(p_home, 3), "options": sorted(opts, key=lambda r: -r["edge"])}
            best = entry["ml"]["options"][0] if entry["ml"]["options"] else None
            if best and best["edge"] >= ML_EDGE and (best["cautious_edge"] or 0) > 0 \
                    and best["p_book"] is not None and best["p_book"] >= best["price"] \
                    and abs(best["p_win"] - best["p_book"]) <= BOOK_AGREE:
                report["picks"].append({"kind": "ml", "game": f'{a["name"]} at {h["name"]}', "ticker": best["ticker"],
                                        "label": f'{best["team"]} to win', "side": "yes", "price": best["price"],
                                        "contracts": best["contracts"], "fee": best["fee"], "p_model": best["p_win"],
                                        "edge": best["edge"], "tip": g["tip"], "book": best["p_book"],
                                        "cautious_edge": best["cautious_edge"]})
        report["games"].append(entry)

    # player props
    for series, comps in SERIES.items():
        for ev in prop_events[series]:
            g = event_game(ev, games)
            entry = next(x for x in report["games"] if x["id"] == g["id"])
            ros = rosters[g["home"]["id"]] + rosters[g["away"]["id"]]
            ladders = {}
            for m in ev.get("markets") or []:
                mt = _PLAYER.match(m.get("yes_sub_title") or "")
                if not mt:
                    continue
                ladders.setdefault(mt.group(1).strip(), []).append((int(mt.group(2)), m))
            for name, rungs in ladders.items():
                pl = find_player(name, ros)
                if not pl:
                    continue
                tid = g["home"]["id"] if any(r["id"] == pl["id"] for r in rosters[g["home"]["id"]]) else g["away"]["id"]
                ti = team_info[tid]
                level = "out" if pl["id"] in {a["id"] for a in ti["absent"]} else "doubt" if norm(pl["name"]) in {norm(d["name"]) for d in ti["doubt"]} else "ok"
                games_p = logs(pl)
                mu, info = projection(games_p, comps, cur_start)
                if mu is None or info.get("history", 0) < MIN_HISTORY:
                    continue
                opp = g["away"]["id"] if tid == g["home"]["id"] else g["home"]["id"]
                f_match, f_rest = matchup_factor(comps, opp, cur, last), rest_factor(games_p, g.get("tip"))
                mu *= f_match * f_rest
                info = {**info, "matchup": round(f_match, 3), "rest": f_rest}
                row = {"player": pl["name"], "stat": LABEL[series], "projection": round(mu, 1), **info,
                       "status": {"out": "out", "doubt": "uncertain"}.get(level, "healthy"), "rungs": []}
                best = None
                for k, m in sorted(rungs):
                    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
                    p = prob_at_least(games_p, comps, mu, info["minutes"], k)
                    if p is None or bid is None or ask is None or ask <= 0:
                        continue
                    mid = (bid + ask) / 2
                    p_cal = mid + SHRINK * (p - mid)                    # the same forecast, pulled toward Kalshi's own price
                    es = [e for e in side_edges(p, bid, ask) if (ask - bid) <= MAX_SPREAD]
                    ec = [e for e in side_edges(p_cal, bid, ask) if (ask - bid) <= MAX_SPREAD]
                    e0 = es      # minutes are never boosted for lineup news, so the no-guess version is the same forecast
                    rung = {"line": k, "bid": bid, "ask": ask, "kalshi_mid": round(mid, 3), "p_model": round(p, 3),
                            "p_cautious": round(p_cal, 3), "best": es[0] if es else None, "ticker": m["ticker"]}
                    row["rungs"].append(rung)
                    # a pick needs the raw model to clear the bar AND the cautious version to still agree on the side
                    # and, where lineup news moved his minutes, the version WITHOUT that guess has to agree on the side too
                    if es and es[0]["edge"] >= MIN_EDGE and ec and ec[0]["side"] == es[0]["side"] and ec[0]["edge"] > 0 \
                            and e0 and e0[0]["side"] == es[0]["side"] and e0[0]["edge"] > 0.02 \
                            and (best is None or es[0]["edge"] > best[1]["edge"]):
                        best = (rung, {**es[0], "cautious_edge": ec[0]["edge"], "base_edge": e0[0]["edge"]})
                fit = min(row["rungs"], key=lambda r: abs(r["line"] - mu), default=None)
                row["fit_line"] = None if fit is None else {k: fit[k] for k in ("line", "kalshi_mid", "p_model")}
                entry["props"].append(row)
                if best and level == "ok":                           # never pick a player who is out or whose minutes are uncertain
                    rung, e = best
                    played = [x for x in games_p if x["MIN"] >= 8]
                    season = [x for x in played if x["date"][:10] >= cur_start]
                    pool = season if len(season) >= 3 else played[-15:]
                    mom = momentum.features([value(x, comps) for x in pool], [value(x, comps) for x in played], UNIT_NBA.get(series, ""),
                                            [x["MIN"] for x in pool], [x["MIN"] for x in played])
                    report["picks"].append({"momentum": mom, "kind": "prop", "game": f'{g["away"]["name"]} at {g["home"]["name"]}', "ticker": rung["ticker"],
                                            "label": f'{pl["name"]} {rung["line"]}+ {LABEL[series]} ({e["side"].upper()})', "side": e["side"],
                                            "price": e["price"], "contracts": e["contracts"], "fee": e["fee"], "p_model": e["p_win"],
                                            "edge": e["edge"], "tip": g["tip"], "projection": row["projection"],
                                            "cautious_edge": e["cautious_edge"], "base_edge": e["base_edge"], "minutes": info["minutes"]})
    # the best picks that are NOT all the same bet: capped per game, per player, per kind of stat, and per direction
    kept, counts = [], {}
    for pk in sorted(report["picks"], key=lambda r: -r["edge"]):
        player, stat, side = label_parts(pk["label"], pk["kind"])
        side = side or ("yes" if pk["kind"] == "ml" else None)
        if not allowed(counts, pk["game"], player, stat, side):
            continue
        tally(counts, pk["game"], player, stat, side)
        kept.append(pk)
    report["picks"] = kept
    return report


# ---------- the shadow account --------------------------------------------------------------------------------

def record_picks(store, report, now=None):
    """Save tonight's picks once each, only before tip-off. Returns how many were new."""
    now = int(time.time() if now is None else now)
    store.x(SCHEMA)
    new = 0
    for pk in report["picks"]:
        tip = None
        try:
            tip = int(datetime.fromisoformat(str(pk["tip"]).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            pass
        if tip is not None and tip <= now:
            continue                              # never "pick" a game that has started
        if store.q("SELECT 1 FROM nba_picks WHERE ticker=?", (pk["ticker"],)):
            continue
        counts = {}
        for r in store.q("SELECT label, kind, side FROM nba_picks WHERE game=?", (pk["game"],)):
            player, stat, side = label_parts(r["label"], r["kind"])
            tally(counts, pk["game"], player, stat, side or r["side"])
        player, stat, side = label_parts(pk["label"], pk["kind"])
        if not allowed(counts, pk["game"], player, stat, side or pk["side"]):
            continue                              # the game already has its mix from earlier passes
        before = store.q("SELECT COUNT(*) AS n FROM nba_picks")[0]["n"]
        store.x("""INSERT OR IGNORE INTO nba_picks (ticker, ts, game, kind, label, side, price, contracts, stake, fee,
                   p_model, edge, tip_ts, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
                (pk["ticker"], now, pk["game"], pk["kind"], pk["label"], pk["side"], pk["price"], pk["contracts"],
                 round(pk["contracts"] * pk["price"], 2), pk["fee"], pk["p_model"], pk["edge"], tip))
        new += store.q("SELECT COUNT(*) AS n FROM nba_picks")[0]["n"] - before
        momentum.save(store, pk["ticker"], "NBA", pk.get("momentum"), now)
    return new


def settle(store, client, now=None):
    """Close picks whose Kalshi market has a result. Returns how many closed."""
    now = int(time.time() if now is None else now)
    store.x(SCHEMA)
    closed = 0
    for r in store.q("SELECT * FROM nba_picks WHERE status='open' AND COALESCE(tip_ts, 0) < ?", (now - 3 * 3600,)):
        try:
            res = client.market(r["ticker"]).get("result")
        except Exception:
            continue
        if res not in ("yes", "no"):
            continue
        won = res == r["side"]
        pnl = (r["contracts"] * (1 - r["price"]) - r["fee"]) if won else -(r["stake"] + r["fee"])
        store.x("UPDATE nba_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE ticker=?",
                (res, round(pnl, 2), now, r["ticker"]))
        closed += 1
    return closed


def run(store, client, now=None, every=7200, fetch=requests.get):
    """One scheduled pass: settle old picks, then (at most every `every` seconds) read tonight and record picks.
    The report is kept in kv for the app. Never raises."""
    now = int(time.time() if now is None else now)
    try:
        settle(store, client, now)
        if now - float(store.get_kv("nba_last_run", 0) or 0) < every:
            return None
        store.set_kv("nba_last_run", now)
        report = build_report(client, store, now, fetch=fetch)
        report["recorded"] = record_picks(store, report, now)
        store.set_kv("nba_tonight", report)
        return report
    except Exception:
        return None


def summary(store, now=None):
    """For the app: tonight's report plus the shadow account's record."""
    store.x(SCHEMA)
    rep = store.get_kv("nba_tonight") or {}
    rows = store.q("SELECT * FROM nba_picks ORDER BY ts")
    done = [r for r in rows if r["status"] == "settled"]
    acct = {"picks": len(rows), "settled": len(done), "open": len(rows) - len(done)}
    if done:
        staked = sum(r["stake"] + r["fee"] for r in done)
        pnl = sum(r["pnl"] for r in done)
        acct.update({"pnl": round(pnl, 2), "wins": sum(1 for r in done if r["pnl"] > 0), "per100": round(100 * pnl / staked, 2) if staked else None})
    return {"tonight": rep, "account": acct}
