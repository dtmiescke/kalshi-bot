"""Which kinds of bets have actually paid? Every slice is tested, and the bar rises with the number of slices.

Takes the settled paper bets on 15-minute markets and splits them by coin, side, price band, how far into
the window the bet was placed, and order style. For each slice it reports the average return per dollar
staked and how many standard errors that is from zero. With this many slices some will look good by
luck, so a slice is called "paying" only if its lower bound clears zero at the bar for the whole family
(hindsight.family_z over every slice tried), and "losing" only if its upper bound is below zero."""
import math
import time
from datetime import datetime, timezone

from . import entry_timing, hindsight

MIN_BETS = 30
LOOKBACK_DAYS = 5      # a blocked kind of bet stops adding losses, so old ones must age out for it to be retested
PRICE_BANDS = ((0.0, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 0.75), (0.75, 1.01))
TIME_BANDS = ((0, 3, "first 3 min"), (3, 6, "3–6 min in"), (6, 9, "6–9 min in"), (9, 15, "9+ min in"))
SESSIONS = ((0, 7, "Asia hours (00–07 UTC)"), (7, 13, "Europe hours (07–13 UTC)"), (13, 21, "US hours (13–21 UTC)"), (21, 24, "Late US (21–24 UTC)"))
COINS = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXBNB15M": "BNB"}


def _band(price):
    for lo, hi in PRICE_BANDS:
        if lo <= price < hi:
            return f"{round(lo * 100)}–{min(round(hi * 100), 100)}¢ bought"


def _minute_in(r):
    close = entry_timing.close_ts(r["ticker"])
    if close is None:
        return None
    mins_in = 15.0 - (close - r["ts"]) / 60.0
    for lo, hi, name in TIME_BANDS:
        if lo <= mins_in < hi:
            return name


def _session(ts):
    h = datetime.fromtimestamp(float(ts), timezone.utc).hour
    return next((n for lo, hi, n in SESSIONS if lo <= h < hi), None)


def keys(r):
    """The slices one bet belongs to: [(dimension, label)]."""
    out = [("all bets", "all bets"), ("style", (r["style"] or "taker") + " orders")]
    if r["series"] in COINS:
        out.append(("coin", COINS[r["series"]]))
    out.append(("side", "bought " + (r["side"] or "?").upper()))
    if r["price"] is not None:
        out.append(("price", _band(float(r["price"]) if float(r["price"]) <= 1 else float(r["price"]) / 100)))
    t = _minute_in(r)
    if t:
        out.append(("time", t))
    if r["ts"] is not None:
        out.append(("session", _session(r["ts"])))
    return [(d, l) for d, l in out if l]


def table(store, now=None):
    now = time.time() if now is None else now
    rows = store.q("""SELECT ticker, ts, series, side, price, stake, pnl, style FROM bets
                      WHERE mode='paper' AND status='graded' AND series LIKE 'KX%15M' AND stake > 0 AND pnl IS NOT NULL AND ts >= ?""", (now - LOOKBACK_DAYS * 86400,))
    groups = {}
    for r in rows:
        for k in keys(r):
            groups.setdefault(k, []).append(float(r["pnl"]) / float(r["stake"]))
    tested = [k for k, v in groups.items() if len(v) >= MIN_BETS and k[0] != "all bets"]
    z = hindsight.family_z(max(1, len(tested)))
    out = []
    for (dim, label), v in groups.items():
        n = len(v)
        mean = sum(v) / n
        se = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1) / n) if n > 1 else 0.0
        bar = z if (dim, label) in tested else hindsight.family_z(1)
        lcb, ucb = mean - bar * se, mean + bar * se
        state = "learning" if n < MIN_BETS else "paying" if lcb > 0 else "losing" if ucb < 0 else "no proof"
        out.append({"dim": dim, "label": label, "n": n, "mean": round(mean, 4), "z": round(mean / se, 2) if se else None,
                    "state": state})
    out.sort(key=lambda s: (s["dim"] != "all bets", s["dim"], -s["n"]))
    return {"slices": out, "bets": len(rows), "tested": len(tested), "needed_z": round(z, 2), "min_bets": MIN_BETS, "ts": int(now)}


def summary(store):
    return table(store)


GATED = ("coin", "price", "time", "session")           # only these can stop a bet; side/style/overall are for reading


def blocked(tbl, series, side, price, mins_in, now=None):
    """The reason to skip a bet, or None. Only a slice proven to lose (past the whole-family bar) blocks."""
    if not tbl:
        return None
    mine = {("coin", COINS.get(series)), ("price", _band(price if price <= 1 else price / 100)),
            ("time", next((n for lo, hi, n in TIME_BANDS if lo <= mins_in < hi), None)),
            ("session", _session(time.time() if now is None else now))}
    for s in tbl["slices"]:
        if s["dim"] in GATED and s["state"] == "losing" and (s["dim"], s["label"]) in mine:
            return f"{s['dim']} '{s['label']}': {s['n']} settled bets averaged {s['mean'] * 100:+.0f}% per dollar"
    return None
