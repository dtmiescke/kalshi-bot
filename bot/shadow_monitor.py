"""One compact view of every shadow account for the app: NBA picks, NFL picks and the bet combos built from them.

Read-only. It joins the three tables the trackers already keep and returns short rows the phone can list: totals per lane,
the picks (open first, then the latest settled) and every combo with the live status of each of its legs."""
import json
import re

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


def _row(sport, r):
    return {"sport": sport, "label": r["label"], "game": r["game"], "side": r["side"], "price": r["price"],
            "p": r["p_model"], "edge": r["edge"], "status": r["status"], "pnl": r["pnl"], "tip": r["tip_ts"],
            "kind": r["kind"]}


def report(store, now=None):
    lanes, picks, status = {}, [], {}
    for sport, table in TABLES:
        rows = _q(store, f"SELECT * FROM {table}")
        for r in rows:
            status[r["ticker"]] = ("won" if r["pnl"] > 0 else "lost") if r["status"] == "settled" and r["pnl"] is not None else "open"
        if sport == "NBA":
            lanes["NBA props"] = _lane([r for r in rows if r["kind"] == "prop"])
            lanes["NBA game winners"] = _lane([r for r in rows if r["kind"] != "prop"])
        else:
            lanes["NFL props"] = _lane(rows)
        opened = sorted((r for r in rows if r["status"] == "open"), key=lambda r: (r["tip_ts"] or 0, -(r["edge"] or 0)))[:OPEN_CAP]
        settled = sorted((r for r in rows if r["status"] == "settled"), key=lambda r: -(r["settled_ts"] or 0))[:SETTLED_CAP]
        picks += [_row(sport, r) for r in opened + settled]
    combos = []
    crows = _q(store, "SELECT * FROM sports_combos ORDER BY (status='open') DESC, ts DESC, edge DESC")
    lanes["Bet combos"] = _lane(crows)
    for r in crows[:COMBO_CAP]:
        try:
            legs = json.loads(r["legs"])
        except ValueError:
            continue
        combos.append({"status": r["status"], "pnl": r["pnl"], "price": r["price"], "chance": r["p_cautious"], "edge": r["edge"],
                       "stake": r["stake"], "tip": r["tip_ts"],
                       "legs": [{"label": g["label"], "price": g["price"], "status": status.get(g["ticker"], "open")} for g in legs]})
    return {"lanes": lanes, "picks": picks, "combos": combos}
