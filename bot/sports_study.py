"""Does the sportsbook consensus beat Kalshi's own price on sports games?

The sports side bets when the devigged sportsbook consensus disagrees with Kalshi's price. That only pays if the
books are better informed than Kalshi's market, so this measures it directly on every settled game where both were
recorded: log loss of each against what happened, and the profit of buying the side the books prefer when they are
at least GAP apart (bought at Kalshi's mid + 1 cent, real taker fee). Per league, with the bar raised for the number
of leagues compared (hindsight.family_z). No bets are placed or changed here."""
import math
import time

from . import hindsight, risk

GAP = 0.05
MIN_GAMES = 30


def _ll(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -math.log(p if y else 1 - p)


def _stats(v):
    n = len(v)
    if n < 2:
        return n, 0.0, 0.0
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return n, m, (m / (sd / math.sqrt(n)) if sd else 0.0)


def _pairs(store):
    """[(league, book_prob, kalshi_prob, outcome)] for settled sports markets that have both signals."""
    rows = store.q("""SELECT segment AS ticker, signal, prob, outcome FROM signal_grades
                      WHERE segment LIKE 'KX%GAME-%' AND signal IN ('book_consensus', 'kalshi_market')
                      AND outcome IN (0, 1) AND prob IS NOT NULL""")
    by = {}
    for r in rows:
        by.setdefault(r["ticker"], {})[r["signal"]] = (float(r["prob"]), int(r["outcome"]))
    return [(t.split("-")[0], v["book_consensus"][0], v["kalshi_market"][0], v["kalshi_market"][1])
            for t, v in by.items() if "book_consensus" in v and "kalshi_market" in v]


def _summary(pairs):
    n = len(pairs)
    diff = [_ll(k, y) - _ll(b, y) for _, b, k, y in pairs]            # positive: the books were closer to the truth
    _, gain, z = _stats(diff)
    far = [(b, k, y) for _, b, k, y in pairs if abs(b - k) >= GAP]
    pnl = []
    for b, k, y in far:
        yes = b > k
        cost = min((k if yes else 1 - k) + 0.01, 0.99)
        fee = risk.taker_fee(100, cost, 0.07) / 100
        pnl.append((1 - cost - fee) if (y == 1) == yes else (-cost - fee))
    fn, fm, fz = _stats(pnl)
    return {"games": n, "book_logloss": round(sum(_ll(b, y) for _, b, _, y in pairs) / n, 4),
            "kalshi_logloss": round(sum(_ll(k, y) for _, _, k, y in pairs) / n, 4),
            "gain": round(gain, 4), "z": round(z, 2),
            "apart": {"games": fn, "per100": round(fm * 200, 2) if fn else None, "z": round(fz, 2) if fn > 1 else None}}


def report(store, now=None):
    """For the app. Safe on an empty table."""
    pairs = _pairs(store)
    out = {"min_games": MIN_GAMES, "gap": GAP, "ts": int(time.time() if now is None else now), "games": len(pairs)}
    if len(pairs) < MIN_GAMES:
        return out
    out["all"] = _summary(pairs)
    leagues = {}
    for p in pairs:
        leagues.setdefault(p[0], []).append(p)
    tested = [k for k, v in leagues.items() if len(v) >= MIN_GAMES]
    out["needed_z"] = round(hindsight.family_z(max(1, len(tested))), 2)
    out["leagues"] = sorted(({"league": k[2:-4] or k, **_summary(v)} for k, v in leagues.items() if len(v) >= MIN_GAMES),
                            key=lambda r: -r["games"])
    return out
