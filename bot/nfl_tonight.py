"""NFL player props against Kalshi's own prices: a graded shadow account. Nothing here ever places a bet.

For each open NFL game ESPN lists, every player Kalshi prices a prop for (passing, receiving and rushing yards,
receptions, passing touchdowns, rushing attempts) gets a projection from free ESPN game logs, a chance for each
Kalshi line, and an edge after the real fee at the real quote. Picks are recorded once, before kickoff, and graded
from Kalshi's own result. Lessons from the NBA tracker are built in: the forecast is pulled harder toward Kalshi's
price before a pick is made, and picks are capped per player and per game, because picks on one game rise and
fall together and a single blowout used to sink a whole night."""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from . import nba_tonight as nba
from .matching import norm

log = logging.getLogger(__name__)

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_LOG = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{id}/gamelog"
CURRENT, LAST = 2026, 2025
CUR_START = "2026-08-01"

# Kalshi series -> (ESPN stat name, label, group)
SERIES = {
    "KXNFLPASSYDS": ("passingYards", "passing yards", "pass"),
    "KXNFLPASSTDS": ("passingTouchdowns", "passing TDs", "pass"),
    "KXNFLRECYDS": ("receivingYards", "receiving yards", "pass"),
    "KXNFLREC": ("receptions", "receptions", "pass"),
    "KXNFLRSHYDS": ("rushingYards", "rushing yards", "rush"),
    "KXNFLRSHATT": ("rushingAttempts", "rushing attempts", "rush"),
}
DECAY = 0.85               # each game back counts this much as the one after it
PRIOR_WEIGHT = 2.0         # last season's average counts as this many recent games
MIN_HISTORY = 8
LEAGUE_TEAM_POINTS = 22.5
SCRIPT_MAX = 0.10          # game script moves a projection by at most 10%
MIN_EDGE = 0.08
MAX_EDGE = 0.35            # a disagreement this big with Kalshi is far likelier a model error than value (NBA: the 25%+ tier did worst)
MAX_FETCH = 60             # game logs fetched per pass; the rest wait for the next pass (they are cached for 6 hours)
MAX_SPREAD = 0.10
SHRINK = 0.5               # the cautious check trusts a disagreement with Kalshi this much (the NBA used 0.35)
STAKE = nba.STAKE
MAX_PER_PLAYER = 2
MAX_PER_GAME = 5
MAX_PER_RUN = 20
SCHEMA = """CREATE TABLE IF NOT EXISTS nfl_picks (
    ticker TEXT PRIMARY KEY, ts INTEGER, game TEXT, kind TEXT, label TEXT, side TEXT, price REAL, contracts INTEGER,
    stake REAL, fee REAL, p_model REAL, edge REAL, tip_ts INTEGER, status TEXT, result TEXT, pnl REAL, settled_ts INTEGER)"""


# ---------- pure maths ----------------------------------------------------------------------------------------

def parse_gamelog(payload):
    """ESPN game-log JSON -> [{"date", "stats": {name: float}}], oldest first. Columns are matched by ESPN's own stat
    names, so a quarterback's rushing columns never get mistaken for a runner's."""
    names = payload.get("names") or []
    meta = payload.get("events") or {}
    out = []
    for st in payload.get("seasonTypes") or []:
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                info = meta.get(str(ev.get("eventId"))) or {}
                stats = {}
                for name, raw in zip(names, ev.get("stats") or []):
                    try:
                        stats[name] = float(str(raw).replace(",", ""))
                    except ValueError:
                        continue
                if stats and info.get("gameDate"):
                    out.append({"date": info["gameDate"][:10], "stats": stats})
    return sorted(out, key=lambda g: g["date"])


def values(games, stat):
    return [g["stats"][stat] for g in games if stat in g["stats"]]


def projection(games, stat, cur_start=CUR_START):
    """(mean, details) from this season's games, recent ones weighted more, with last season as a small prior."""
    cur = values([g for g in games if g["date"] >= cur_start], stat)
    prior = values([g for g in games if g["date"] < cur_start], stat)
    allv = cur + prior
    if len(allv) < 3:
        return None, {}
    weights = [DECAY ** age for age in range(len(cur))][::-1] if cur else []
    num = sum(w * x for w, x in zip(weights, cur))
    den = sum(weights)
    if prior:
        num += PRIOR_WEIGHT * (sum(prior[-17:]) / len(prior[-17:]))
        den += PRIOR_WEIGHT
    mu = num / den
    return mu, {"games_this_season": len(cur), "history": len(allv), "season_avg": round(sum(cur) / len(cur), 1) if cur else None,
                "last3": round(sum(cur[-3:]) / len(cur[-3:]), 1) if cur else None}


def prob_at_least(games, stat, mu, line):
    """Chance of finishing above a Kalshi line (its floor_strike): his own game-to-game spread, restated so its
    average is the projection."""
    hist = values(games, stat)[-34:]
    if len(hist) < MIN_HISTORY:
        return None
    avg = sum(hist) / len(hist)
    if avg <= 0:
        return None
    scale = mu / avg
    hit = sum(1 for x in hist if x * scale > line)
    return (hit + 0.5) / (len(hist) + 1)


def team_totals(over_under, spread):
    """(home points, away points) implied by the total and the home spread (negative when home is favoured)."""
    try:
        ou, sp = float(over_under), float(spread)
    except (TypeError, ValueError):
        return None
    home_margin = -sp
    return (ou + home_margin) / 2, (ou - home_margin) / 2


def script_factor(group, team_points, margin):
    """Game-script multiplier: a team expected to score a lot throws and catches more; a favourite runs more."""
    if team_points is None or margin is None:
        return 1.0
    if group == "pass":
        f = 0.5 * (team_points / LEAGUE_TEAM_POINTS - 1)
    else:
        f = 0.004 * margin
    return 1 + max(-SCRIPT_MAX, min(SCRIPT_MAX, f))


# ---------- ESPN ----------------------------------------------------------------------------------------------

def slate(fetch=requests.get, now=None, days=4):
    """Upcoming NFL games with their DraftKings total and spread: [{"id","tip","state","home","away","ou","spread"}]."""
    start = datetime.fromtimestamp(now or time.time(), timezone.utc)
    games, seen = [], set()
    for offset in range(-1, days):
        data = nba._get(ESPN + "/scoreboard", {"dates": f"{start + timedelta(days=offset):%Y%m%d}"}, fetch)
        for ev in data.get("events") or []:
            if ev.get("id") in seen:
                continue
            seen.add(ev.get("id"))
            comp = (ev.get("competitions") or [{}])[0]
            teams = {c.get("homeAway"): {"id": str(c["team"]["id"]), "abbr": c["team"]["abbreviation"],
                                         "name": c["team"]["displayName"]} for c in comp.get("competitors") or []}
            odds = (comp.get("odds") or [{}])[0]
            if "home" in teams and "away" in teams:
                games.append({"id": ev["id"], "tip": ev.get("date"), "state": ((ev.get("status") or {}).get("type") or {}).get("state"),
                              "ou": odds.get("overUnder"), "spread": odds.get("spread"), **teams})
    return games


def roster(team_id, fetch=requests.get):
    data = nba._get(f"{ESPN}/teams/{team_id}/roster", None, fetch)
    out = []
    for grp in data.get("athletes") or []:
        for a in grp.get("items") or []:
            if isinstance(a, dict) and a.get("id"):
                out.append({"id": a["id"], "name": a.get("displayName") or a.get("fullName"),
                            "pos": (a.get("position") or {}).get("abbreviation")})
    return out


def player_games(athlete_id, fetch=requests.get):
    games = []
    for season in (LAST, CURRENT):
        games += parse_gamelog(nba._get(ESPN_LOG.format(id=athlete_id), {"season": season}, fetch))
        time.sleep(0.15)
    return sorted(games, key=lambda g: g["date"])


# ---------- the report ----------------------------------------------------------------------------------------

def liquid(m):
    """A rung worth rating: both sides quoted, a tight enough book, and a price that is not already a near-certainty."""
    bid, ask = nba._f(m.get("yes_bid_dollars")), nba._f(m.get("yes_ask_dollars"))
    return bid is not None and ask is not None and 0 < bid <= ask and ask - bid <= MAX_SPREAD and 0.05 <= (bid + ask) / 2 <= 0.95


def build_report(client, store=None, now=None, fetch=requests.get):
    now = int(time.time() if now is None else now)
    games = [g for g in slate(fetch, now) if g["state"] == "pre"]
    report = {"ts": now, "games": [], "picks": []}
    if not games:
        return report
    for g in games:
        tot = team_totals(g["ou"], g["spread"])
        g["points"] = {g["home"]["id"]: tot[0], g["away"]["id"]: tot[1]} if tot else {}
        g["margin"] = {g["home"]["id"]: -float(g["spread"]), g["away"]["id"]: float(g["spread"])} if tot else {}
        report["games"].append({"id": g["id"], "tip": g["tip"], "home": g["home"]["name"], "away": g["away"]["name"],
                                "total": g["ou"], "spread": g["spread"]})
    rosters = {}
    for g in games:
        for t in (g["home"], g["away"]):
            if t["id"] not in rosters:
                rosters[t["id"]] = nba._cached(store, f"nfl_roster_{t['id']}", 86400, lambda t=t: roster(t["id"], fetch), now)
    inj = nba._cached(store, "nfl_injuries", 1800, lambda: nba.injuries_feed(fetch, base=ESPN), now)
    logs, fetched = {}, [0]

    def get_logs(pl):
        if pl["id"] not in logs:
            def make():
                fetched[0] += 1
                return player_games(pl["id"], fetch)
            cached = store.get_kv(f"nfl_games_{pl['id']}") if store is not None else None
            if not (isinstance(cached, dict) and now - cached.get("ts", 0) < 6 * 3600) and fetched[0] >= MAX_FETCH:
                return None                          # over budget this pass: try again next pass
            logs[pl["id"]] = nba._cached(store, f"nfl_games_{pl['id']}", 6 * 3600, make, now)
        return logs[pl["id"]]

    candidates = []
    for series, (stat, label, group) in SERIES.items():
        try:
            events = nba.kalshi_events(client, series)
        except Exception:
            continue
        for ev in events:
            g = nba.event_game(ev, games)
            if not g or g["state"] != "pre":
                continue
            ros = rosters[g["home"]["id"]] + rosters[g["away"]["id"]]
            out_names = {norm(r["name"] or "") for t in (g["home"], g["away"]) for r in inj.get(t["name"], []) if r["level"] != "ok"}
            ladders = {}
            for m in ev.get("markets") or []:
                mt = nba._PLAYER.match(m.get("yes_sub_title") or "")
                if mt and m.get("floor_strike") is not None:
                    ladders.setdefault(mt.group(1).strip(), []).append((float(m["floor_strike"]), m))
            for name, rungs in ladders.items():
                pl = nba.find_player(name, ros)
                if not pl or norm(pl["name"]) in out_names:
                    continue
                tid = g["home"]["id"] if any(r["id"] == pl["id"] for r in rosters[g["home"]["id"]]) else g["away"]["id"]
                if not any(liquid(m) for _, m in rungs):
                    continue                         # no quote worth rating: do not spend a game-log fetch on him
                gl = get_logs(pl)
                if not gl:
                    continue
                mu, info = projection(gl, stat)
                if mu is None or info.get("history", 0) < MIN_HISTORY:
                    continue
                f = script_factor(group, g["points"].get(tid), g["margin"].get(tid))
                mu *= f
                best = None
                for line, m in sorted(rungs, key=lambda x: x[0]):
                    bid, ask = nba._f(m.get("yes_bid_dollars")), nba._f(m.get("yes_ask_dollars"))
                    p = prob_at_least(gl, stat, mu, line)
                    if p is None or bid is None or ask is None or ask <= 0 or ask - bid > MAX_SPREAD:
                        continue
                    mid = (bid + ask) / 2
                    es = nba.side_edges(p, bid, ask, stake=STAKE)
                    ec = nba.side_edges(mid + SHRINK * (p - mid), bid, ask, stake=STAKE)
                    if not es or es[0]["edge"] < MIN_EDGE:
                        continue
                    if es[0]["edge"] > MAX_EDGE:
                        report["skipped_extreme"] = report.get("skipped_extreme", 0) + 1
                        continue
                    top = es[0]
                    cautious = next((e for e in ec if e["side"] == top["side"]), None)
                    if cautious is None or cautious["edge"] <= 0:
                        continue
                    if best is None or top["edge"] > best["e"]["edge"]:
                        best = {"e": top, "line": line, "m": m, "cautious": cautious["edge"]}
                if best:
                    e, m = best["e"], best["m"]
                    n = int(round(best["line"] + 0.5))
                    candidates.append({
                        "kind": "prop", "game": f'{g["away"]["name"]} at {g["home"]["name"]}', "game_id": g["id"], "player": pl["name"],
                        "ticker": m["ticker"], "label": f'{pl["name"]} {n}+ {label} ({e["side"].upper()})', "side": e["side"],
                        "price": e["price"], "contracts": e["contracts"], "fee": e["fee"], "p_model": e["p_win"], "edge": e["edge"],
                        "cautious_edge": round(best["cautious"], 4), "tip": g["tip"], "projection": round(mu, 1), "script": round(f, 3),
                        "season_avg": info.get("season_avg"), "last3": info.get("last3")})
    per_player, per_game, picks = {}, {}, []
    for c in sorted(candidates, key=lambda c: -c["edge"]):
        if per_player.get(c["player"], 0) >= MAX_PER_PLAYER or per_game.get(c["game_id"], 0) >= MAX_PER_GAME or len(picks) >= MAX_PER_RUN:
            continue
        per_player[c["player"]] = per_player.get(c["player"], 0) + 1
        per_game[c["game_id"]] = per_game.get(c["game_id"], 0) + 1
        picks.append(c)
    report["picks"] = picks
    return report


def record_picks(store, report, now=None):
    """Record each pick once, and only while the game has not started. Returns how many were new."""
    now = int(time.time() if now is None else now)
    store.x(SCHEMA)
    added = 0
    for p in report.get("picks", []):
        try:
            tip = int(datetime.fromisoformat(str(p["tip"]).replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        if tip <= now:
            continue
        cur = store.x("""INSERT OR IGNORE INTO nfl_picks (ticker, ts, game, kind, label, side, price, contracts, stake, fee,
                         p_model, edge, tip_ts, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                      (p["ticker"], now, p["game"], p["kind"], p["label"], p["side"], p["price"], p["contracts"],
                       round(p["contracts"] * p["price"], 2), p["fee"], p["p_model"], p["edge"], tip))
        added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return added


def settle(store, client, now=None):
    """Close picks whose Kalshi market has a result. Returns how many closed."""
    now = int(time.time() if now is None else now)
    store.x(SCHEMA)
    closed = 0
    for r in store.q("SELECT * FROM nfl_picks WHERE status='open' AND COALESCE(tip_ts, 0) < ?", (now - 3 * 3600,)):
        try:
            res = client.market(r["ticker"]).get("result")
        except Exception:
            continue
        if res not in ("yes", "no"):
            continue
        won = res == r["side"]
        pnl = (r["contracts"] * (1 - r["price"]) - r["fee"]) if won else -(r["stake"] + r["fee"])
        store.x("UPDATE nfl_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE ticker=?",
                (res, round(pnl, 2), now, r["ticker"]))
        closed += 1
    return closed


def run(store, client, now=None, every=7200, fetch=requests.get):
    """One scheduled pass: settle old picks, then (at most every `every` seconds) read the slate and record picks."""
    now = int(time.time() if now is None else now)
    try:
        settle(store, client, now)
        if now - float(store.get_kv("nfl_last_run", 0) or 0) < every:
            return None
        store.set_kv("nfl_last_run", now)
        report = build_report(client, store, now, fetch=fetch)
        report["recorded"] = record_picks(store, report, now)
        store.set_kv("nfl_tonight", report)
        return report
    except Exception:
        log.warning("nfl tracker pass failed", exc_info=True)
        return None


def summary(store, now=None):
    """For the app: the latest slate plus the shadow account's record."""
    store.x(SCHEMA)
    rep = store.get_kv("nfl_tonight") or {}
    rows = store.q("SELECT * FROM nfl_picks ORDER BY ts")
    done = [r for r in rows if r["status"] == "settled"]
    acct = {"picks": len(rows), "settled": len(done), "open": len(rows) - len(done)}
    if done:
        staked = sum(r["stake"] + r["fee"] for r in done)
        pnl = sum(r["pnl"] for r in done)
        acct.update({"pnl": round(pnl, 2), "wins": sum(1 for r in done if r["pnl"] > 0),
                     "per100": round(100 * pnl / staked, 2) if staked else None})
    return {"tonight": rep, "account": acct}
