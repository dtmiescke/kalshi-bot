"""College football moneylines and parlays against Kalshi's prices: a graded shadow account. Nothing here ever bets.

ESPN's free scoreboard carries a DraftKings moneyline for most games. For each game Kalshi also lists (KXNCAAFGAME), the
sportsbook's chance (vig removed) is set against Kalshi's ask. A game's favourite is recorded as a pick when it sits in a
sensible price band, with its edge after the real fee measured against the book, which may be negative: the point of
the account is to find out whether favourites parlays pay, not to pretend an edge. Each day three parlays are built
from the day's picks, one per game, in three tiers (chalk, middle, riskier), priced as the product of the legs' Kalshi
asks plus an assumed margin per leg, and graded from Kalshi's own results."""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from itertools import combinations

import requests

from . import nba_tonight as nba, odds, polymarket, risk
from .espn_odds import SCOREBOARD, event_from_game
from .matching import name_score

log = logging.getLogger(__name__)

PATH = "football/college-football"
SERIES = "KXNCAAFGAME"
PICK_PRICE = (0.40, 0.90)       # a favourite worth a leg: not a near-certainty, not a coin flip against it
MAX_SPREAD = 0.05
MAX_PICKS_PER_RUN = 20
COMBO_MARGIN = 0.02             # extra cost per leg, as in the other combo lane (an assumption)
LEGS = 3
TIERS = (("Chalk", 0.70, 0.90), ("Middle", 0.55, 0.80), ("Riskier", 0.45, 0.70))   # leg price band per tier
STAKE = nba.STAKE
MAX_CONTRACTS = 40
PICK_SCHEMA = """CREATE TABLE IF NOT EXISTS cfb_picks (
    ticker TEXT PRIMARY KEY, ts INTEGER, game TEXT, kind TEXT, label TEXT, side TEXT, price REAL, contracts INTEGER,
    stake REAL, fee REAL, p_model REAL, edge REAL, tip_ts INTEGER, status TEXT, result TEXT, pnl REAL, settled_ts INTEGER)"""
COMBO_SCHEMA = """CREATE TABLE IF NOT EXISTS cfb_combos (
    key TEXT PRIMARY KEY, ts INTEGER, day TEXT, tier TEXT, legs TEXT, price REAL, p_book REAL, contracts INTEGER,
    stake REAL, fee REAL, tip_ts INTEGER, status TEXT, pnl REAL, settled_ts INTEGER)"""


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def book_games(fetch=requests.get, now=None, days=2):
    """[{"id","home","away","tip","p_home","p_away"}] from ESPN's DraftKings lines, vig removed, games not yet started."""
    start = datetime.fromtimestamp(now or time.time(), timezone.utc)
    seen, out = set(), []
    for offset in range(-1, days):
        day = f"{start + timedelta(days=offset):%Y%m%d}"
        r = fetch(SCOREBOARD.format(path=PATH), params={"dates": day, "groups": 80, "limit": 300}, timeout=15)
        r.raise_for_status()
        for g in (r.json() or {}).get("events") or []:
            if g.get("id") in seen:
                continue
            seen.add(g.get("id"))
            ev = event_from_game(g)
            if not ev:
                continue
            outs = ev["bookmakers"][0]["markets"][0]["outcomes"]
            probs = odds.devig_multiplicative([odds.american_to_prob(o["price"]) for o in outs])
            out.append({"id": g["id"], "home": ev["home_team"], "away": ev["away_team"], "tip": ev["commence_time"],
                        "p_home": probs[0], "p_away": probs[1]})
    return out


def match(title, games):
    """The book game a Kalshi title ('Arizona vs Texas Tech') is, or None."""
    import re
    parts = [p.strip() for p in re.split(r"\s+(?:vs\.?|at|@)\s+", str(title or ""))]
    if len(parts) != 2:
        return None
    best = None
    for g in games:
        for a, b in ((g["home"], g["away"]), (g["away"], g["home"])):
            s = min(name_score(parts[0], a), name_score(parts[1], b))
            if s >= 0.6 and (best is None or s > best[0]):
                best = (s, g)
    return best[1] if best else None


def candidates(events, games, fee_rate=0.07, poly=None):
    """One row per game: its book favourite, with Kalshi's ask, the book's chance and the edge after the fee."""
    out = []
    for ev in events:
        g = match(ev.get("title"), games)
        if not g:
            continue
        rows = []
        for m in ev.get("markets") or []:
            label = m.get("yes_sub_title") or ""
            home = name_score(label, g["home"]) >= name_score(label, g["away"])
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if bid is None or ask is None or not 0 < bid <= ask < 1 or ask - bid > MAX_SPREAD:
                continue
            p = g["p_home"] if home else g["p_away"]
            contracts = max(1, min(MAX_CONTRACTS, int(STAKE // ask)))
            fee = risk.taker_fee(contracts, ask, fee_rate)
            p2 = polymarket.chance(poly, label, g["away"] if home else g["home"]) if poly else None
            rows.append({"ticker": m["ticker"], "team": label, "p_poly": p2, "game": f'{g["away"]} at {g["home"]}', "gid": g["id"], "tip": g["tip"],
                         "price": ask, "p_book": p, "contracts": contracts, "fee": fee,
                         "edge": p - ask - fee / contracts})
        if rows:
            out.append(max(rows, key=lambda r: r["p_book"]))        # the favourite
    return [c for c in out if PICK_PRICE[0] <= c["price"] <= PICK_PRICE[1]]


def build_combos(cands, legs=LEGS, tiers=TIERS):
    """One parlay per tier: the legs in its price band whose combined book chance is highest, one per game, none reused
    across tiers when enough other games exist. Priced at the product of asks plus COMBO_MARGIN per leg."""
    used, out = set(), []
    for name, lo, hi in tiers:
        pool = sorted((c for c in cands if lo <= c["price"] <= hi), key=lambda c: -c["p_book"])[:12]
        best = None
        for combo in combinations(pool, legs):
            if len({c["gid"] for c in combo}) < legs:
                continue
            fresh = sum(1 for c in combo if c["ticker"] not in used)
            score = (fresh, prod(c["p_book"] for c in combo))
            if best is None or score > best[0]:
                best = (score, combo)
        if not best:
            continue
        combo = best[1]
        used.update(c["ticker"] for c in combo)
        cost = prod(min(0.99, c["price"] + COMBO_MARGIN) for c in combo)
        contracts = max(1, min(MAX_CONTRACTS, int(STAKE // cost)))
        out.append({"tier": name, "legs": list(combo), "price": cost, "p_book": prod(c["p_book"] for c in combo),
                    "contracts": contracts, "fee": risk.taker_fee(contracts, min(max(cost, 0.01), 0.99), 0.07)})
    return out


def prod(xs):
    r = 1.0
    for x in xs:
        r *= x
    return r


def _tip(iso):
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def record(store, cands, combos, now):
    """Save picks and combos once each, only before kickoff. Returns (new picks, new combos)."""
    store.x(PICK_SCHEMA)
    store.x(COMBO_SCHEMA)
    picks = 0
    for c in cands[:MAX_PICKS_PER_RUN]:
        tip = _tip(c["tip"])
        if tip is not None and tip <= now:
            continue
        cur = store.x("""INSERT OR IGNORE INTO cfb_picks (ticker, ts, game, kind, label, side, price, contracts, stake, fee, p_model, edge,
                         tip_ts, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
                      (c["ticker"], int(now), c["game"], "ml", f'{c["team"]} to win', "yes", round(c["price"], 4), c["contracts"],
                       round(c["contracts"] * c["price"], 2), c["fee"], round(c["p_book"], 4), round(c["edge"], 4), tip))
        picks += max(0, cur.rowcount or 0)
    new = 0
    day = f"{datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d}"
    for cb in combos:
        tips = [t for t in (_tip(c["tip"]) for c in cb["legs"]) if t]
        if tips and min(tips) <= now:
            continue
        legs = [{k: c.get(k) for k in ("ticker", "team", "game", "price", "p_book", "p_poly")} for c in cb["legs"]]
        key = day + "|" + "|".join(sorted(c["ticker"] for c in cb["legs"]))
        cur = store.x("""INSERT OR IGNORE INTO cfb_combos (key, ts, day, tier, legs, price, p_book, contracts, stake, fee, tip_ts, status)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')""",
                      (key, int(now), day, cb["tier"], json.dumps(legs), round(cb["price"], 5), round(cb["p_book"], 4),
                       cb["contracts"], round(cb["contracts"] * cb["price"], 2), cb["fee"], min(tips) if tips else None))
        new += max(0, cur.rowcount or 0)
    return picks, new


def settle(store, client, now=None):
    """Close picks whose Kalshi market has a result, then combos: one lost leg loses it, every leg won wins it."""
    now = int(time.time() if now is None else now)
    store.x(PICK_SCHEMA)
    store.x(COMBO_SCHEMA)
    closed = 0
    for r in store.q("SELECT * FROM cfb_picks WHERE status='open' AND COALESCE(tip_ts, 0) < ?", (now - 3 * 3600,)):
        try:
            res = client.market(r["ticker"]).get("result")
        except Exception:
            continue
        if res not in ("yes", "no"):
            continue
        pnl = (r["contracts"] * (1 - r["price"]) - r["fee"]) if res == r["side"] else -(r["stake"] + r["fee"])
        store.x("UPDATE cfb_picks SET status='settled', result=?, pnl=?, settled_ts=? WHERE ticker=?", (res, round(pnl, 2), now, r["ticker"]))
        closed += 1
    for r in store.q("SELECT * FROM cfb_combos WHERE status='open'"):
        results = []
        for leg in json.loads(r["legs"]):
            row = store.q("SELECT status, pnl FROM cfb_picks WHERE ticker=?", (leg["ticker"],))
            results.append(None if not row or row[0]["status"] != "settled" else row[0]["pnl"] > 0)
        if any(x is False for x in results):
            pnl = -(r["stake"] + r["fee"])
        elif all(x is True for x in results):
            pnl = r["contracts"] * (1 - r["price"]) - r["fee"]
        else:
            continue
        store.x("UPDATE cfb_combos SET status='settled', pnl=?, settled_ts=? WHERE key=?", (round(pnl, 2), now, r["key"]))
        closed += 1
    return closed


def run(store, client, now=None, every=3600, fetch=requests.get):
    """One throttled pass. Never raises, never places anything."""
    now = int(time.time() if now is None else now)
    try:
        settle(store, client, now)
        if now - float(store.get_kv("cfb_last_run", 0) or 0) < every:
            return None
        store.set_kv("cfb_last_run", now)
        games = book_games(fetch, now)
        cands = candidates(nba.kalshi_events(client, SERIES), games, poly=polymarket.cached(store, 'cfb', now=now))
        combos = build_combos(cands)
        picks, new = record(store, cands, combos, now)
        return {"games": len(games), "candidates": len(cands), "picks": picks, "combos": new}
    except Exception:
        log.warning("college football pass failed", exc_info=True)
        return None


def summary(store, now=None):
    store.x(PICK_SCHEMA)
    store.x(COMBO_SCHEMA)

    def acct(rows):
        done = [r for r in rows if r["status"] == "settled"]
        a = {"n": len(rows), "settled": len(done), "open": len(rows) - len(done)}
        if done:
            staked = sum(r["stake"] + r["fee"] for r in done)
            pnl = sum(r["pnl"] for r in done)
            a.update({"pnl": round(pnl, 2), "wins": sum(1 for r in done if r["pnl"] > 0),
                      "per100": round(100 * pnl / staked, 2) if staked else None})
        return a

    picks = store.q("SELECT * FROM cfb_picks ORDER BY ts DESC")
    combos = store.q("SELECT * FROM cfb_combos ORDER BY ts DESC")
    cur = [{"tier": r["tier"], "price": r["price"], "p_book": r["p_book"], "stake": r["stake"],
            "legs": [{"team": g["team"], "game": g["game"], "price": g["price"], "p_poly": g.get("p_poly")} for g in json.loads(r["legs"])]}
           for r in combos if r["status"] == "open"][:6]
    return {"picks": acct(picks), "combos": acct(combos), "open_combos": cur}
