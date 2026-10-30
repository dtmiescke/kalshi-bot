"""Every strategy on one yardstick.

The bot runs several lanes (15-minute crypto, ladders, sportsbook bets, NBA moneylines and props, the loosened-rules
experiment). Each used to be judged its own way. This puts them side by side with the same three questions:

* dollars: net dollars, and dollars per $100 staked (a few big wins can fake a positive total, so the return per
  dollar is what counts);
* is it real: a 95% interval for the return per dollar from a cluster bootstrap (picks on the same game, or bets in the
  same hour, move together, so whole clusters are resampled, which keeps the interval honest);
* does the forecast beat the market: where a lane records its own win probability, its Brier score is compared with
  the one a bettor gets from simply believing the price.

Verdicts are fixed in advance. A lane is Proven only when it has at least MIN_BETS settled bets over at least
MIN_CLUSTERS clusters, the interval's low end is above zero, AND it is still ahead after its DROP_TOP best bets are
removed (otherwise Fragile: a few lucky wins carry it); Losing when the high end is below zero; otherwise Not proven,
or Too early under the minimums. This reports; it never changes a setting or a stake."""
import math
import random

MIN_BETS = 100
MIN_CLUSTERS = 20
DROP_TOP = 10
RESAMPLES = 2000
SEED = 20261030
LADDERS = ("KXBTCD", "KXETHD")


def _ci(rows, resamples=RESAMPLES, seed=SEED):
    """(low, high) 95% interval for pooled return per dollar, resampling whole clusters. rows: [(cluster, stake, pnl)]."""
    groups = {}
    for key, stake, pnl in rows:
        g = groups.setdefault(key, [0.0, 0.0])
        g[0] += stake
        g[1] += pnl
    cl = list(groups.values())
    if len(cl) < 2:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        stake = pnl = 0.0
        for _ in cl:
            s, p = cl[rng.randrange(len(cl))]
            stake += s
            pnl += p
        if stake > 0:
            means.append(pnl / stake)
    if not means:
        return None, None
    means.sort()
    return means[int(0.025 * len(means))], means[min(len(means) - 1, int(0.975 * len(means)))]


def verdict(n, clusters, low, high, ex_top=None):
    if n < MIN_BETS or clusters < MIN_CLUSTERS or low is None:
        return "too_early"
    if low > 0:
        return "proven" if ex_top is None or ex_top > 0 else "fragile"
    if high < 0:
        return "losing"
    return "not_proven"


def lane(name, rows, probs=None):
    """One lane. rows: [(cluster, stake, pnl)]; probs: optional [(p_model, price, won)] on the side bought."""
    n = len(rows)
    stake = sum(r[1] for r in rows)
    pnl = sum(r[2] for r in rows)
    wins = sum(1 for r in rows if r[2] > 0)
    low, high = _ci(rows) if n >= 2 else (None, None)
    clusters = len({r[0] for r in rows})
    best_first = sorted((r[2] for r in rows), reverse=True)
    ex_top = sum(best_first[DROP_TOP:]) if n > DROP_TOP else None
    out = {"lane": name, "n": n, "wins": wins, "clusters": clusters, "dollars": round(pnl, 2),
           "staked": round(stake, 2), "per100": round(100 * pnl / stake, 2) if stake else None,
           "ci_low": round(100 * low, 1) if low is not None else None,
           "ci_high": round(100 * high, 1) if high is not None else None,
           "ex_top": round(ex_top, 2) if ex_top is not None else None,
           "verdict": verdict(n, clusters, low, high, ex_top)}
    if probs:
        m = len(probs)
        out["forecast"] = round(100 * sum(p for p, _, _ in probs) / m, 1)
        out["hit"] = round(100 * sum(w for _, _, w in probs) / m, 1)
        bm = sum((p - w) ** 2 for p, _, w in probs) / m
        bk = sum((c - w) ** 2 for _, c, w in probs) / m
        out["brier_model"] = round(bm, 4)
        out["brier_market"] = round(bk, 4)
        out["skill"] = round(1 - bm / bk, 3) if bk else None       # above 0: the forecast beat the price
    return out


def _q(store, sql, args=()):
    try:
        return store.q(sql, args)
    except Exception:
        return []                      # a lane whose table does not exist yet is simply empty


def report(store):
    lanes = []
    bets = _q(store, """SELECT series, event_ticker, ts, stake, pnl FROM bets
                        WHERE mode='paper' AND status='graded' AND stake > 0 AND pnl IS NOT NULL""")

    def bet_rows(sel):
        return [(r["event_ticker"] or r["ts"] // 3600, float(r["stake"]), float(r["pnl"])) for r in bets if sel(r["series"] or "")]

    lanes.append(lane("15-minute crypto", bet_rows(lambda s: s.startswith("KX") and s.endswith("15M"))))
    lanes.append(lane("Hourly and daily ladders", bet_rows(lambda s: s in LADDERS)))
    lanes.append(lane("Sportsbook-based bets", bet_rows(lambda s: not (s.endswith("15M") or s in LADDERS))))
    picks = _q(store, """SELECT game, kind, price, stake, p_model, pnl FROM nba_picks
                         WHERE status='settled' AND stake > 0 AND pnl IS NOT NULL""")
    for name, sel in (("NBA moneylines (shadow)", lambda k: k != "prop"), ("NBA player props (shadow)", lambda k: k == "prop")):
        sub = [r for r in picks if sel(r["kind"] or "")]
        lanes.append(lane(name, [(r["game"], float(r["stake"]), float(r["pnl"])) for r in sub],
                          [(float(r["p_model"]), float(r["price"]), 1.0 if r["pnl"] > 0 else 0.0) for r in sub
                           if r["p_model"] is not None and r["price"] is not None]))
    nfl = _q(store, """SELECT game, price, stake, p_model, pnl FROM nfl_picks
                       WHERE status='settled' AND stake > 0 AND pnl IS NOT NULL""")
    lanes.append(lane("NFL player props (shadow)", [(r["game"], float(r["stake"]), float(r["pnl"])) for r in nfl],
                      [(float(r["p_model"]), float(r["price"]), 1.0 if r["pnl"] > 0 else 0.0) for r in nfl
                       if r["p_model"] is not None and r["price"] is not None]))
    cmb = _q(store, """SELECT ts, price, stake, p_cautious, pnl FROM sports_combos
                       WHERE status='settled' AND stake > 0 AND pnl IS NOT NULL""")
    lanes.append(lane("Bet combos, several players (shadow)", [(r["ts"] // 86400, float(r["stake"]), float(r["pnl"])) for r in cmb],
                      [(float(r["p_cautious"]), float(r["price"]), 1.0 if r["pnl"] > 0 else 0.0) for r in cmb
                       if r["p_cautious"] is not None and r["price"] is not None]))
    exp = _q(store, """SELECT ts, price, stake, p_win, pnl FROM experiment_bets
                       WHERE status='settled' AND stake > 0 AND pnl IS NOT NULL""")
    lanes.append(lane("Loosened-rules experiment (shadow)", [(r["ts"] // 3600, float(r["stake"]), float(r["pnl"])) for r in exp],
                      [(float(r["p_win"]), float(r["price"]), 1.0 if r["pnl"] > 0 else 0.0) for r in exp
                       if r["p_win"] is not None and r["price"] is not None]))
    return {"min_bets": MIN_BETS, "min_clusters": MIN_CLUSTERS, "lanes": [x for x in lanes if x["n"]],
            "proven": [x["lane"] for x in lanes if x["verdict"] == "proven"]}
