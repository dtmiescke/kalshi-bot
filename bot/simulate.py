"""Simulated markets with a known truth, to find the right betting formula and how long it takes to know.

World: each window has a true up-probability t. Kalshi's price is the truth plus market error (logit noise
sk). The bot's forecast is the truth plus its own error (logit noise sf), independent of the market's. Then
the best probability to bet on is the precision-weighted mix of the two, which is Kalshi's price moved toward
the forecast by lam* = sk^2 / (sk^2 + sf^2) of the gap. If the market is never wrong (sk = 0) lam* is 0 and
no forecast can help: every bet only pays fees. This module checks that closed form against brute-force
profit, checks that the bot's own estimator (brain.shrink_weight) recovers it, says how many bets it takes
to tell an edge from luck, and bounds sk from the real settled windows.

The simulation says what a formula does when the assumptions hold. It cannot show that Kalshi is beatable:
only the real settled windows can, which is what the other studies measure."""
import math
import os
import random
import tempfile

from . import brain, risk

BARS = (0.02, 0.03, 0.05, 0.08)
LAMS = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)
SPREAD = 0.02
SLIPPAGE = 0.005


def _sig(x):
    return 1 / (1 + math.exp(-x))


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def world(sk, sf, n, seed):
    """[(kalshi_price, bot_forecast, outcome)] for n windows."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        t = _sig(rng.gauss(0, 1.0))                          # truth: windows range from lopsided to even
        k = min(max(_sig(_logit(t) + rng.gauss(0, sk)), 0.03), 0.97)
        p = min(max(_sig(_logit(t) + rng.gauss(0, sf)), 0.02), 0.98)
        out.append((k, p, int(rng.random() < t)))
    return out


def lambda_star(sk, sf):
    """The share of (forecast - price) to believe, when both are the truth plus independent error."""
    return sk ** 2 / (sk ** 2 + sf ** 2) if (sk or sf) else 0.0


def pnl(rows, bar, lam, fee_rate=0.07):
    """Profit per $1 contract for each trade the rule takes: belief q = k + lam*(p-k), buy a side whose
    win chance beats ask + fee by the bar."""
    out = []
    for k, p, y in rows:
        q = k + lam * (p - k)
        for yes, win_p, ask in ((True, q, k + SPREAD / 2), (False, 1 - q, 1 - k + SPREAD / 2)):
            cost = min(ask + SLIPPAGE, 0.99)
            fee = risk.taker_fee(100, cost, fee_rate) / 100
            if win_p - cost - fee >= bar:
                out.append((1 - cost - fee) if (y == 1) == yes else (-cost - fee))
                break
    return out


def stats(v):
    n = len(v)
    if n < 2:
        return {"n": n, "mean": 0.0, "se": 0.0, "sd": 0.0}
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return {"n": n, "mean": m, "se": sd / math.sqrt(n), "sd": sd}


def best_rule(rows):
    """(bar, lam, stats) with the highest mean profit per contract among rules with 200+ trades."""
    best = None
    for bar in BARS:
        for lam in LAMS:
            s = stats(pnl(rows, bar, lam))
            if s["n"] >= 200 and (best is None or s["mean"] > best[2]["mean"]):
                best = (bar, lam, s)
    return best


def bets_to_detect(mean, sd, z=2.7):
    """Trades needed before an average profit of `mean` per contract is z standard errors from zero."""
    return None if mean <= 0 else int(math.ceil((z * sd / mean) ** 2))


def estimator_check(sk, sf, n_windows=600, seed=1):
    """What the bot's own shrink estimator learns from n_windows simulated settled windows."""
    from .storage import Store
    store = Store(os.path.join(tempfile.mkdtemp(), "sim.sqlite"))
    rows = world(sk, sf, n_windows, seed)
    for i, (k, p, y) in enumerate(rows):
        t = f"KXBTC15M-26OCT28{i // 100:02d}{i % 100:02d}-00"
        store.q("""INSERT INTO decision_audit (ts, ticker, event_ticker, series, decision, market_price, model_prob,
                   outcome, brier) VALUES (?,?,?,?,?,?,?,?,?)""",
                (1_700_000_000 + i * 900, t, t, "KXBTC15M", "research", k, p, y, (p - y) ** 2))
    return brain.shrink_weight(store, "KXBTC15M", now=1.0)


def market_gap(rows):
    """Kalshi's log loss minus the loss it would have if its own price were the truth. Zero for an efficient
    market; the excess is what a better forecaster could at most win back. [(price, outcome)] in."""
    if not rows:
        return None
    loss = sum(-math.log(k if y else 1 - k) for k, y in rows) / len(rows)
    ent = sum(-(k * math.log(k) + (1 - k) * math.log(1 - k)) for k, _ in rows) / len(rows)
    se = math.sqrt(sum((-math.log(k if y else 1 - k) + (k * math.log(k) + (1 - k) * math.log(1 - k)) - (loss - ent)) ** 2
                       for k, y in rows) / max(1, len(rows) - 1) / len(rows))
    return {"windows": len(rows), "excess_logloss": loss - ent, "se": se}


def lambda_fit(rows):
    """(estimate, standard error) of the share of (forecast - price) that is real, by the same method as
    brain.shrink_weight but without its prior, from [(price, forecast, outcome)] with one row per window."""
    wins = [((p - k) * (y - k), (p - k) ** 2) for k, p, y in rows]
    den = sum(d for _, d in wins)
    if len(wins) < 3 or den <= 0:
        return None
    est = sum(a for a, _ in wins) / den
    var = sum((a - est * d) ** 2 for a, d in wins) / den ** 2
    return est, math.sqrt(var)


def windows_to_pin(se_now, n_now, target_se=0.1):
    """Windows needed for the standard error of lam to reach target_se, if it shrinks like 1/sqrt(n)."""
    return None if not se_now else int(math.ceil(n_now * (se_now / target_se) ** 2))


def report(store, per_day=None):
    """What the real settled windows say about whether there is anything to find. For the app."""
    import time
    from . import online
    rows = [(_sig(f["kalshi_logit"]), y) for _, f, y in online.decisions(store)]
    gap = market_gap(rows)
    out = {"ts": int(time.time()), "market_gap": None, "lam_used": round(brain.shrink_weight(store, "KXBTC15M"), 3)}
    if gap and gap["windows"] >= 100:
        out["market_gap"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in gap.items()}
    audit = store.q("""SELECT ticker, model_prob, market_price, outcome FROM decision_audit
                       WHERE brier IS NOT NULL AND outcome IN (0,1) AND model_prob IS NOT NULL AND series LIKE 'KX%15M'
                       AND market_price BETWEEN ? AND ?""", brain.LEARN_BAND)
    first = {}
    for r in audit:
        first.setdefault(r["ticker"], []).append((float(r["market_price"]), float(r["model_prob"]), int(r["outcome"])))
    per = [(sum(a for a, _, _ in v) / len(v), sum(b for _, b, _ in v) / len(v), v[0][2]) for v in first.values()]
    fit = lambda_fit(per)
    if fit and len(per) >= 30:
        est, se = fit
        out["lam_fit"] = {"windows": len(per), "estimate": round(est, 3), "se": round(se, 3),
                          "windows_for_pm0_1": windows_to_pin(se, len(per))}
    return out


def scenario_table(n=30000, seed=7):
    out = []
    for sk, sf in ((0.0, 0.3), (0.05, 0.3), (0.1, 0.3), (0.2, 0.3), (0.2, 0.6), (0.3, 0.3)):
        rows = world(sk, sf, n, seed)
        best = best_rule(rows)
        entry = {"sk": sk, "sf": sf, "lam_star": round(lambda_star(sk, sf), 3)}
        for name, lam in (("closed_form", lambda_star(sk, sf)), ("trust_forecast", 1.0)):
            s = stats(pnl(rows, 0.05, lam))
            entry[name] = {"n": s["n"], "mean": round(s["mean"], 4), "bets_to_detect": bets_to_detect(s["mean"], s["sd"])}
        if best:
            entry["best"] = {"bar": best[0], "lam": best[1], "n": best[2]["n"], "mean": round(best[2]["mean"], 4),
                             "bets_to_detect": bets_to_detect(best[2]["mean"], best[2]["sd"])}
        out.append(entry)
    return out


if __name__ == "__main__":
    for e in scenario_table():
        print(e)
    for sk, sf in ((0.1, 0.3), (0.2, 0.3), (0.0, 0.3)):
        print("estimator", sk, sf, "true", round(lambda_star(sk, sf), 3), "learned", round(estimator_check(sk, sf, 1000), 3))
