"""Which settings would have worked, tested so that luck cannot pass.

Tries every combination of a few settings on the settled windows (the edge bar the forecast must clear,
and how far the forecast is pulled toward Kalshi's price), picks the best on the older 60% of windows,
and reports how that same choice did on the newer 40% it never saw. A good in-sample number that
collapses out of sample is what overfitting looks like, and with this much data it is the usual outcome.
The best in-sample z is also reported against the number of combinations tried (family_z), so a result
only counts as real if it beats what the best of that many coin flips would show.

This is a plain exhaustive search; with this few settings it finds everything a smarter optimiser
(Optuna and the like) would, without a heavy dependency or a way to hide the number of tries."""
import math

from . import hindsight, online, risk

EDGE_BARS = (0.03, 0.05, 0.08, 0.12)
SHRINKS = (0.1, 0.3, 0.6, 1.0)
MIN_TRADES = 30
TRAIN_SHARE = 0.6


def _prepare(store):
    """[(ts, p_formula, kalshi_mid, spread, y)] oldest first, one per window at its earliest decision."""
    out = []
    for ts, f, y in online.decisions(store):
        if f.get("model_prob") is None:
            continue
        k = 1 / (1 + math.exp(-f["kalshi_logit"]))
        out.append((ts, float(f["model_prob"]), k, float(f.get("kalshi_spread") or 0.02), int(y)))
    return out


def _pnl(rows, bar, lam, fee_rate=0.07):
    """Profit per $1 contract of each trade the settings would have taken."""
    out = []
    for _, p, k, spread, y in rows:
        q = k + lam * (p - k)
        for side_yes, win_p, ask in ((True, q, k + spread / 2), (False, 1 - q, 1 - k + spread / 2)):
            cost = min(ask + 0.005, 0.99)
            fee = risk.taker_fee(100, cost, fee_rate) / 100
            if win_p - cost - fee >= bar:
                out.append((1 - cost - fee) if (y == 1) == side_yes else (-cost - fee))
                break
    return out


def _stats(pnl):
    n = len(pnl)
    if n < 2:
        return n, None, None
    m = sum(pnl) / n
    se = math.sqrt(sum((v - m) ** 2 for v in pnl) / (n - 1) / n)
    return n, m, (m / se if se else None)


def run(rows):
    """Returns the table of settings, the pick on the older windows and its result on the newer ones."""
    cut = int(len(rows) * TRAIN_SHARE)
    train, test = rows[:cut], rows[cut:]
    tried = []
    for bar in EDGE_BARS:
        for lam in SHRINKS:
            n, m, z = _stats(_pnl(train, bar, lam))
            tn, tm, tz = _stats(_pnl(test, bar, lam))
            tried.append({"bar": bar, "shrink": lam, "train_n": n, "train_mean": m, "train_z": z,
                          "test_n": tn, "test_mean": tm, "test_z": tz})
    ok = [t for t in tried if t["train_n"] >= MIN_TRADES and t["train_mean"] is not None]
    best = max(ok, key=lambda t: t["train_mean"]) if ok else None
    need = hindsight.family_z(len(tried))
    verdict = "too few windows"
    if best and best["test_n"] >= MIN_TRADES:
        real = best["train_z"] is not None and best["train_z"] >= need and best["test_mean"] > 0 \
            and best["test_z"] is not None and best["test_z"] >= 2.0
        verdict = "holds up" if real else "does not hold up"
    return tried, best, need, verdict


def report(store):
    rows = _prepare(store)
    tried, best, need, verdict = run(rows)
    rnd = lambda v, d=4: None if v is None else round(v, d)
    return {"windows": len(rows), "combinations": len(tried), "needed_z": rnd(need, 2), "verdict": verdict,
            "best": None if not best else {k: (rnd(v) if isinstance(v, float) else v) for k, v in best.items()}}
