"""Is a player hot or cold going into the game, and does that beat the price? Recorded for every pick, judged later.

Nothing here changes a pick. For each NBA or NFL prop pick it stores how the player's last 3 games compare with his season
average (Hot at 15% or more above it, Cold at 15% or more below), his last 5 games, and for basketball his minutes trend.
`study` then compares picks that go WITH the momentum (a hot player on an over, a cold player on an under) against picks
that go against it, on how often they won against the price paid. Kalshi keeps no pre-game prices of finished prop
markets, so this can only be tested forward, from the picks the bot records."""
import json
import random

HOT, COLD = 1.15, 0.85
MIN_N = 100
SCHEMA = """CREATE TABLE IF NOT EXISTS pick_features (ticker TEXT PRIMARY KEY, sport TEXT, ts INTEGER, feature TEXT)"""


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def features(season, recent, unit="", minutes_season=None, minutes_recent=None):
    """season: values this season oldest first (or his recent games if the season is young); recent: his latest games.
    Returns {"trend", "ratio", "season_avg", "last3", "last5", "unit", "min_trend"} or None with fewer than 3 games."""
    if len(season) < 3 or len(recent) < 3:
        return None
    s_avg, l3 = _avg(season), _avg(recent[-3:])
    ratio = (l3 / s_avg) if s_avg and s_avg > 0 else None
    trend = None if ratio is None else "hot" if ratio >= HOT else "cold" if ratio <= COLD else "steady"
    out = {"trend": trend, "ratio": None if ratio is None else round(ratio, 3), "season_avg": round(s_avg, 1), "last3": round(l3, 1),
           "last5": [round(v, 1) for v in recent[-5:]], "unit": unit, "min_trend": None}
    if minutes_season and minutes_recent and len(minutes_recent) >= 3:
        out["min_trend"] = round(_avg(minutes_recent[-3:]) - _avg(minutes_season), 1)
    return out


def save(store, ticker, sport, feature, now):
    if not feature:
        return
    store.x(SCHEMA)
    store.x("INSERT OR IGNORE INTO pick_features VALUES (?,?,?,?)", (ticker, sport, int(now), json.dumps(feature)))


def load(store):
    try:
        return {r["ticker"]: json.loads(r["feature"]) for r in store.q("SELECT ticker, feature FROM pick_features")}
    except Exception:
        return {}


def alignment(trend, side, label):
    """'with' when the pick goes along with the momentum, 'against' when it fights it, 'steady' when there is none."""
    if trend in (None, "steady"):
        return "steady"
    over = (side or "").lower() == "yes"                        # YES on "N+" is the over
    return "with" if (trend == "hot") == over else "against"


def _ci(rows, resamples=1000, seed=7):
    """95% interval for the mean of (won - price), resampling whole games. rows: [(game, edge)]."""
    groups = {}
    for g, e in rows:
        groups.setdefault(g, []).append(e)
    cl = list(groups.values())
    if len(cl) < 2:
        return None, None
    rng, means = random.Random(seed), []
    for _ in range(resamples):
        tot = cnt = 0
        for _ in cl:
            xs = cl[rng.randrange(len(cl))]
            tot += sum(xs)
            cnt += len(xs)
        means.append(tot / cnt)
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def study(store):
    """Settled picks that have momentum recorded, grouped by whether they go with it. Safe on an empty database."""
    feats = load(store)
    groups = {"with": [], "against": [], "steady": []}
    for table in ("nba_picks", "nfl_picks"):
        try:
            rows = store.q(f"SELECT ticker, game, side, label, price, pnl, status FROM {table} WHERE status='settled' AND pnl IS NOT NULL")
        except Exception:
            continue
        for r in rows:
            f = feats.get(r["ticker"])
            if not f:
                continue
            won = 1.0 if r["pnl"] > 0 else 0.0
            groups[alignment(f.get("trend"), r["side"], r["label"])].append((r["game"], won - r["price"], won, r["price"]))
    out = {"min_n": MIN_N, "groups": {}, "tracked": len(feats)}
    for name, rows in groups.items():
        if not rows:
            out["groups"][name] = {"n": 0}
            continue
        low, high = _ci([(g, e) for g, e, _, _ in rows])
        out["groups"][name] = {"n": len(rows), "hit": round(100 * sum(w for _, _, w, _ in rows) / len(rows), 1),
                               "price": round(100 * sum(p for _, _, _, p in rows) / len(rows), 1),
                               "edge": round(100 * sum(e for _, e, _, _ in rows) / len(rows), 1),
                               "low": None if low is None else round(100 * low, 1), "high": None if high is None else round(100 * high, 1)}
    w = out["groups"]["with"]
    out["verdict"] = ("collecting" if w.get("n", 0) < MIN_N or w.get("low") is None else
                      "helps" if w["low"] > 0 else "no help" if w["high"] < 0 else "not proven")
    return out
