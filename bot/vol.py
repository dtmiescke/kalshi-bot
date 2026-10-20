"""Volatility forecaster: the single most important number for 15-minute
crypto contracts (how far can price travel before the bell?).

Blends four free views:
1. EWMA realized vol: recent minutes weighted most (reacts fast)
2. 60-minute realized vol (stable)
3. Option-implied vol (Deribit DVOL): the market's forward-looking view
4. Time-of-day profile learned from history (US open is wild, Asia lunch is calm)
...then scales up around scheduled macro events (CPI, Fed, jobs reports).

The blend weights are learned: after each settled window, the forecaster checks
which view best predicted the move that actually happened.
"""
import math
import time

import numpy as np

MINUTES_PER_YEAR = 525600
DEFAULT_BLEND = {"ewma": 0.4, "long": 0.3, "implied": 0.3}


def ewma_vol(closes, lam=0.94):
    r = np.diff(np.log(np.asarray(closes, dtype=float)))
    if len(r) < 10:
        return None
    v = r[:10].var()
    for x in r[10:]:
        v = lam * v + (1 - lam) * x * x
    return max(math.sqrt(v), 1e-6)


def long_vol(closes, n=60):
    c = np.asarray(closes[-(n + 1):], dtype=float)
    if len(c) < 20:
        return None
    return max(float(np.diff(np.log(c)).std(ddof=1)), 1e-6)


def hour_profile(store, coin):
    """{hour: multiplier} learned from history (1.0 = average hour)."""
    return store.get_kv(f"vol_profile_{coin}") or {}


def learn_hour_profile(store, coin, candles):
    """From history candles: average |1-minute move| per UTC hour vs overall."""
    if len(candles) < 2000:
        return None
    closes = np.array([c["close"] for c in candles])
    hours = np.array([(c["ts"] % 86400) // 3600 for c in candles[1:]])
    moves = np.abs(np.diff(np.log(closes)))
    overall = moves.mean()
    prof = {str(h): float(moves[hours == h].mean() / overall)
            for h in range(24) if (hours == h).sum() > 30}
    store.set_kv(f"vol_profile_{coin}", prof)
    return prof


def event_multiplier(events, now=None, before_min=30, after_min=60):
    """events: [{"ts": epoch, "impact": "High"|...}]. Vol is higher just before
    and well after high-impact releases."""
    now = now or time.time()
    m = 1.0
    for e in events or []:
        dt = (now - e["ts"]) / 60
        if -before_min <= dt <= after_min:
            m = max(m, 1.8 if e.get("impact") == "High" else 1.25)
    return m


def forecast(store, coin, closes, dvol_pct=None, now=None, events=None):
    """Per-minute volatility forecast + the pieces that made it."""
    now = now or time.time()
    parts = {"ewma": ewma_vol(closes), "long": long_vol(closes),
             "implied": (dvol_pct / 100 / math.sqrt(MINUTES_PER_YEAR)) if dvol_pct else None}
    blend = store.get_kv(f"vol_blend_{coin}") or DEFAULT_BLEND
    avail = {k: v for k, v in parts.items() if v}
    if not avail:
        return None, parts
    wsum = sum(blend.get(k, 0.0) for k in avail) or 1.0
    # blend variances, not standard deviations
    var = sum(blend.get(k, 0.0) / wsum * v * v for k, v in avail.items())
    hour = str(int((now % 86400) // 3600))
    season = hour_profile(store, coin).get(hour, 1.0)
    # realized views already contain today's regime; only the implied view
    # needs the time-of-day adjustment, so apply half of it overall
    season_adj = 1 + 0.5 * (season - 1)
    ev = event_multiplier(events, now)
    sigma = math.sqrt(var) * season_adj * ev
    parts.update({"blend": blend, "season": round(season, 3), "event_mult": ev,
                  "sigma": sigma})
    return sigma, parts


def tail_shrink(store, coin):
    return float(store.get_kv(f"tail_shrink_{coin}") or 0.9)


def learn_tail_shrink(store, coin, rows):
    """rows: [(formula_prob_made_with_0.9, outcome)]. Crypto has fat tails; the
    right amount of shrink is measured from real settled windows, not assumed."""
    from statistics import NormalDist
    nd = NormalDist()
    zs = [(nd.inv_cdf(min(max(p, 1e-4), 1 - 1e-4)) / 0.9, y) for p, y in rows]
    if len(zs) < 200:
        return None
    best, best_ll = 0.9, 1e9
    for a in [x / 100 for x in range(50, 126, 2)]:
        ll = 0.0
        for z, y in zs:
            p = min(max(nd.cdf(a * z), 1e-4), 1 - 1e-4)
            ll -= y * math.log(p) + (1 - y) * math.log(1 - p)
        if ll < best_ll:
            best, best_ll = a, ll
    store.set_kv(f"tail_shrink_{coin}", best)
    return best


def learn_blend(store, coin, samples, lr=0.05):
    """samples: [(parts_dict, realized_move_per_sqrt_min)]. Shift blend weight
    toward whichever view predicted the realized move best (squared error)."""
    blend = dict(store.get_kv(f"vol_blend_{coin}") or DEFAULT_BLEND)
    err = {k: [] for k in blend}
    for parts, realized in samples:
        for k in blend:
            if parts.get(k):
                err[k].append((parts[k] - realized) ** 2)
    scores = {k: (sum(v) / len(v)) for k, v in err.items() if v}
    if len(scores) < 2:
        return blend
    best = min(scores.values()) + 1e-18
    for k, s in scores.items():
        blend[k] = max(0.05, blend[k] * math.exp(-lr * (s / best - 1)))
    tot = sum(blend.values())
    blend = {k: v / tot for k, v in blend.items()}
    store.set_kv(f"vol_blend_{coin}", blend)
    return blend
