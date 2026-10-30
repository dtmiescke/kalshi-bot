"""Bet combos: several single-player picks bundled into one multi-leg bet, as a graded shadow account.

A combo pays $1 per contract only if every leg wins. Each combo is built from open NBA and NFL shadow picks, with one
leg per game (so the legs do not move together) and a different player in every leg. These are synthetic: they are
priced as the product of the legs' Kalshi prices, not as native Kalshi combo orders (those carry their own pricing),
so the dollars are a research estimate of how bundling would have done. Nothing is ever bet.

The odds side is unforgiving: combining legs multiplies any error in each leg's chance, so every leg's chance is first
pulled halfway toward Kalshi's price, each leg is charged an extra COMBO_MARGIN (a combo's quoter keeps a margin the
single markets do not), and a combo is only kept if its cautious chance still beats that cost and is likely enough to be
worth betting. Kalshi's own combo collections are priced by market makers on request, with no public quote to read, so the
margin is an assumption, set on the cautious side."""
import json
import logging
import time
from itertools import combinations

from . import risk

log = logging.getLogger(__name__)

SIZES = (2, 3)              # no long shots: a 4-leg bundle rarely pays
PER_SIZE = 2                # combos kept per size, best first
MIN_LEG_EDGE = 0.08
MAX_LEG_EDGE = 0.20         # a leg this far from Kalshi's price is more likely a model error than value
LEG_PRICE = (0.15, 0.85)    # no near-certainties (nothing to win) and no long shots (rarely hit)
COMBO_MARGIN = 0.02         # extra cost per leg: whoever quotes a combo keeps a margin that single markets do not charge
MIN_CHANCE = 0.30           # the combo must still be likely enough to be worth betting (cautious chance of every leg hitting)
MAX_LEG_USES = 2            # a leg may appear in at most this many combos, so the day is not one bet reshuffled
# ... and two combos share at most one leg
TRUST = 0.5                 # a leg's chance is price + TRUST x (model - price) before it is multiplied
MIN_EDGE = 0.03             # the combo's cautious edge after fees
STAKE = 5.0
MAX_CONTRACTS = 40
POOL = 24                   # legs considered (the best by cautious edge)
SCHEMA = """CREATE TABLE IF NOT EXISTS sports_combos (
    key TEXT PRIMARY KEY, ts INTEGER, n_legs INTEGER, legs TEXT, price REAL, p_model REAL, p_cautious REAL, edge REAL,
    contracts INTEGER, stake REAL, fee REAL, tip_ts INTEGER, status TEXT, pnl REAL, settled_ts INTEGER)"""


def cautious(p, price):
    return price + TRUST * (p - price)


def leg_ok(pk):
    return (pk.get("player") is not None and MIN_LEG_EDGE <= (pk.get("edge") or 0) <= MAX_LEG_EDGE
            and LEG_PRICE[0] <= (pk.get("price") or 0) <= LEG_PRICE[1] and pk.get("p_model") is not None)


def stat_of(label):
    import re
    m = re.match(r"^.*? \d+\+ (.+?) \((?:YES|NO)\)$", label or "")
    return m.group(1) if m else (label or "")


def price_combo(legs, fee_rate=0.07):
    """Cost, model chance, cautious chance, edge after fee, contracts and fee for a list of legs (dicts with price, p_model)."""
    cost = p_model = p_c = 1.0
    for leg in legs:
        cost *= min(0.99, leg["price"] + COMBO_MARGIN)
        p_model *= leg["p_model"]
        p_c *= cautious(leg["p_model"], leg["price"])
    contracts = max(1, min(MAX_CONTRACTS, int(STAKE // cost)))
    fee = risk.taker_fee(contracts, min(max(cost, 0.01), 0.99), fee_rate)
    return {"price": cost, "p_model": p_model, "p_cautious": p_c, "contracts": contracts, "fee": fee,
            "edge": p_c - cost - fee / contracts}


def build(picks, sizes=SIZES, per_size=PER_SIZE):
    """Combos from picks: [{"ticker","game","player","side","label","price","p_model","edge","tip"}]. Props only, one
    leg per game and per player, a different kind of stat in every leg, and never every leg leaning the same way. A leg is
    reused in at most MAX_LEG_USES combos. Best cautious edge first."""
    pool = sorted((p for p in picks if leg_ok(p)), key=lambda p: -(cautious(p["p_model"], p["price"]) - p["price"]))[:POOL]
    uses, out = {}, []
    for size in sizes:
        found = []
        for legs in combinations(pool, size):
            if len({p["game"] for p in legs}) < size or len({p["player"] for p in legs}) < size:
                continue
            if len({stat_of(p["label"]) for p in legs}) < size:
                continue
            sides = [p["side"] for p in legs]
            if size >= 3 and max(sides.count("yes"), sides.count("no")) > (size * 2) // 3:
                continue                                             # a bundle that is all one direction is one bet in disguise
            pr = price_combo(legs)
            if pr["edge"] >= MIN_EDGE and pr["p_cautious"] >= MIN_CHANCE:
                found.append({"legs": list(legs), **pr})
        found.sort(key=lambda c: -c["edge"])
        chosen = 0
        for c in found:
            if chosen >= per_size:
                break
            if any(uses.get(p["ticker"], 0) >= MAX_LEG_USES for p in c["legs"]):
                continue
            mine = {p["ticker"] for p in c["legs"]}
            if any(len(mine & {p["ticker"] for p in o["legs"]}) > 1 for o in out):
                continue                                             # two combos sharing two legs are the same bet twice
            for p in c["legs"]:
                uses[p["ticker"]] = uses.get(p["ticker"], 0) + 1
            out.append(c)
            chosen += 1
    return out


def _pool_from_db(store, now):
    picks = []
    for table, sport in (("nba_picks", "NBA"), ("nfl_picks", "NFL")):
        try:
            rows = store.q(f"SELECT * FROM {table} WHERE status='open' AND COALESCE(tip_ts, 0) > ?", (int(now),))
        except Exception:
            continue
        for r in rows:
            player = None
            if r["kind"] == "prop":
                import re
                m = re.match(r"(.+?) \d+\+", r["label"] or "")
                player = m.group(1) if m else None
            side = r["side"]
            picks.append({"ticker": r["ticker"], "game": f"{sport}:{r['game']}", "player": player, "side": side, "label": r["label"],
                          "price": r["price"], "p_model": r["p_model"], "edge": r["edge"], "tip": r["tip_ts"], "sport": sport,
                          "table": table})
    return picks


def record(store, combos, now):
    """Save combos once each (a leg set is one combo), only while every game is still ahead. Returns how many were new."""
    store.x(SCHEMA)
    added = 0
    for c in combos:
        key = "|".join(sorted(p["ticker"] for p in c["legs"]))
        tip = min((p["tip"] for p in c["legs"] if p.get("tip")), default=None)
        if tip is not None and tip <= now:
            continue
        legs = [{k: p.get(k) for k in ("ticker", "label", "side", "price", "p_model", "game", "table")} for p in c["legs"]]
        cur = store.x("""INSERT OR IGNORE INTO sports_combos (key, ts, n_legs, legs, price, p_model, p_cautious, edge, contracts,
                         stake, fee, tip_ts, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                      (key, int(now), len(legs), json.dumps(legs), round(c["price"], 5), round(c["p_model"], 4),
                       round(c["p_cautious"], 4), round(c["edge"], 4), c["contracts"], round(c["contracts"] * c["price"], 2),
                       round(c["fee"], 2), tip))
        added += max(0, cur.rowcount or 0)
    return added


def settle(store, now=None):
    """A combo loses the moment a leg loses and wins when every leg has won. Legs are read from the pick tables."""
    now = int(time.time() if now is None else now)
    store.x(SCHEMA)
    closed = 0
    for r in store.q("SELECT * FROM sports_combos WHERE status='open'"):
        legs = json.loads(r["legs"])
        results = []
        for leg in legs:
            try:
                row = store.q(f"SELECT status, pnl FROM {leg['table']} WHERE ticker=?", (leg["ticker"],))
            except Exception:
                row = []
            results.append(None if not row or row[0]["status"] != "settled" else row[0]["pnl"] > 0)
        if any(x is False for x in results):
            pnl = -(r["stake"] + r["fee"])
        elif all(x is True for x in results):
            pnl = r["contracts"] * (1 - r["price"]) - r["fee"]
        else:
            continue
        store.x("UPDATE sports_combos SET status='settled', pnl=?, settled_ts=? WHERE key=?", (round(pnl, 2), now, r["key"]))
        closed += 1
    return closed


def run(store, now=None, every=3600):
    """One throttled pass: settle finished combos, then build and record new ones from the open picks. Never raises."""
    now = int(time.time() if now is None else now)
    try:
        store.x(SCHEMA)
        settle(store, now)
        if now - float(store.get_kv("sports_combos_last", 0) or 0) < every:
            return None
        store.set_kv("sports_combos_last", now)
        combos = build(_pool_from_db(store, now))
        return {"built": len(combos), "recorded": record(store, combos, now)}
    except Exception:
        log.warning("sports combos pass failed", exc_info=True)
        return None


def summary(store, now=None):
    store.x(SCHEMA)
    rows = store.q("SELECT * FROM sports_combos ORDER BY ts DESC, edge DESC")
    done = [r for r in rows if r["status"] == "settled"]
    openr = [r for r in rows if r["status"] == "open"]
    acct = {"combos": len(rows), "settled": len(done), "open": len(openr)}
    if done:
        staked = sum(r["stake"] + r["fee"] for r in done)
        pnl = sum(r["pnl"] for r in done)
        acct.update({"pnl": round(pnl, 2), "wins": sum(1 for r in done if r["pnl"] > 0),
                     "per100": round(100 * pnl / staked, 2) if staked else None})
    cur = [{"legs": [{"label": g["label"], "price": g["price"]} for g in json.loads(r["legs"])], "price": r["price"],
            "p_model": r["p_model"], "p_cautious": r["p_cautious"], "edge": r["edge"], "stake": r["stake"]} for r in openr[:12]]
    return {"account": acct, "open": cur}
