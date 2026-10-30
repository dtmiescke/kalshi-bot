"""One compact view of every shadow account for the app: NBA picks, NFL picks and the bet combos built from them.

Read-only. It joins the three tables the trackers already keep and returns short rows the phone can list: totals per lane,
the picks (open first, then the latest settled) and every combo with the live status of each of its legs."""
import hashlib
import json

OPEN_CAP = 70            # open picks listed per sport
SETTLED_CAP = 40         # latest settled picks listed per sport
COMBO_CAP = 40
TABLES = (("NBA", "nba_picks"), ("NFL", "nfl_picks"))


def _q(store, sql, args=()):
    try:
        return store.q(sql, args)
    except Exception:
        return []                     # a tracker that has not run yet has no table: its list is simply empty


def _lane(rows):
    done = [r for r in rows if r["status"] == "settled"]
    out = {"picks": len(rows), "open": len(rows) - len(done), "settled": len(done)}
    if done:
        staked = sum(r["stake"] + r["fee"] for r in done)
        pnl = sum(r["pnl"] for r in done)
        out.update({"wins": sum(1 for r in done if r["pnl"] > 0), "pnl": round(pnl, 2),
                    "per100": round(100 * pnl / staked, 1) if staked else None})
    return out


def side_now(quote, side):
    """Kalshi's current chance for the side that was bought: the mid of the YES quote, or one minus it for NO."""
    if not quote:
        return None
    mid = (quote["bid"] + quote["ask"]) / 2
    return round(mid if side == "yes" else 1 - mid, 4)


def _row(sport, r, quotes, progress):
    quote = quotes.get(r["ticker"])
    win = (r["contracts"] * (1 - r["price"]) - r["fee"]) if r["contracts"] else None
    return {"id": r["ticker"], "sport": sport, "label": r["label"], "game": r["game"], "side": r["side"], "price": r["price"],
            "p": r["p_model"], "edge": r["edge"], "status": r["status"], "pnl": r["pnl"], "tip": r["tip_ts"],
            "kind": r["kind"], "ts": r["ts"], "contracts": r["contracts"], "stake": r["stake"], "fee": r["fee"],
            "win": round(win, 2) if win is not None else None, "lose": round(r["stake"] + r["fee"], 2),
            "now": side_now(quote, r["side"]), "now_ts": quote["ts"] if quote else None, "prog": progress.get(r["ticker"])}


def report(store, now=None):
    lanes, picks, status = {}, [], {}
    try:
        quotes = store.get_kv("pick_quotes", {}) or {}
    except Exception:
        quotes = {}
    try:
        progress = store.get_kv("pick_progress", {}) or {}
    except Exception:
        progress = {}
    sides = {}
    for sport, table in TABLES:
        rows = _q(store, f"SELECT * FROM {table}")
        for r in rows:
            sides[r["ticker"]] = r["side"]
            status[r["ticker"]] = ("won" if r["pnl"] > 0 else "lost") if r["status"] == "settled" and r["pnl"] is not None else "open"
        if sport == "NBA":
            lanes["NBA props"] = _lane([r for r in rows if r["kind"] == "prop"])
            lanes["NBA game winners"] = _lane([r for r in rows if r["kind"] != "prop"])
        else:
            lanes["NFL props"] = _lane(rows)
        opened = sorted((r for r in rows if r["status"] == "open"), key=lambda r: (r["tip_ts"] or 0, -(r["edge"] or 0)))[:OPEN_CAP]
        settled = sorted((r for r in rows if r["status"] == "settled"), key=lambda r: -(r["settled_ts"] or 0))[:SETTLED_CAP]
        picks += [_row(sport, r, quotes, progress) for r in opened + settled]
    combos = []
    crows = _q(store, "SELECT * FROM sports_combos ORDER BY (status='open') DESC, ts DESC, edge DESC")
    lanes["Bet combos"] = _lane(crows)
    for r in crows[:COMBO_CAP]:
        try:
            legs = json.loads(r["legs"])
        except ValueError:
            continue
        live = [side_now(quotes.get(g["ticker"]), g.get("side") or sides.get(g["ticker"], "yes")) for g in legs]
        now_chance = None
        if all(x is not None for x in live):
            now_chance = 1.0
            for x in live:
                now_chance *= x
        win = r["contracts"] * (1 - r["price"]) - r["fee"]
        combos.append({"id": hashlib.sha1(r["key"].encode()).hexdigest()[:10], "status": r["status"], "pnl": r["pnl"],
                       "price": r["price"], "chance": r["p_cautious"], "model": r["p_model"], "edge": r["edge"],
                       "stake": r["stake"], "fee": r["fee"], "contracts": r["contracts"], "win": round(win, 2),
                       "lose": round(r["stake"] + r["fee"], 2), "tip": r["tip_ts"], "ts": r["ts"], "now": None if now_chance is None else round(now_chance, 4),
                       "now_ts": min((quotes[g["ticker"]]["ts"] for g in legs if g["ticker"] in quotes), default=None),
                       "legs": [{"id": g["ticker"], "label": g["label"], "price": g["price"], "p": g.get("p_model"), "status": status.get(g["ticker"], "open"),
                                 "now": x, "prog": progress.get(g["ticker"])} for g, x in zip(legs, live)]})
    return {"lanes": lanes, "picks": picks, "combos": combos}
