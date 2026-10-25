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


# Bounds on the fat-tail adjustment. Below 1.0 fattens the tails; an unbounded
# fit once reached 0.5, which priced a ~1% long shot at ~10-13% and bought it.
DEFAULT_TAIL_SHRINK = 0.9
TAIL_SHRINK_MIN, TAIL_SHRINK_MAX = 0.8, 1.05


def _bounded_shrink(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TAIL_SHRINK
    if not math.isfinite(v) or v <= 0:
        return DEFAULT_TAIL_SHRINK
    return min(max(v, TAIL_SHRINK_MIN), TAIL_SHRINK_MAX)


def tail_shrink(store, coin):
    """Learned shrink, clamped so a stale or bad stored fit can't distort pricing."""
    return _bounded_shrink(store.get_kv(f"tail_shrink_{coin}"))


MIN_HOLDOUT_WINDOWS = 50
MIN_FIT_WINDOWS = 50
HOLDOUT_FRACTION = 0.3


def _window_logloss(a, by_window, nd):
    """Mean per-window log loss at shrink `a`; each window counts once."""
    total = 0.0
    for rows in by_window:
        ll = 0.0
        for z, y in rows:
            p = min(max(nd.cdf(a * z), 1e-4), 1 - 1e-4)
            ll -= y * math.log(p) + (1 - y) * math.log(1 - p)
        total += ll / len(rows)
    return total / len(by_window)


def learn_tail_shrink(store, coin, rows, now=None):
    """Fit the fat-tail shrink from settled windows, and keep it only if it
    beats the 0.9 default on NEWER windows it never saw.

    rows: [(window_ticker, ts, live_z, outcome)], where live_z is the un-shrunk
    z the bot actually priced with (settlement.z_above, logged by crypto_brain).
    Windows are split in time order: the newest HOLDOUT_FRACTION (at least
    MIN_HOLDOUT_WINDOWS) are held out. The candidate is fitted on the older
    windows within [TAIL_SHRINK_MIN, TAIL_SHRINK_MAX]; it is stored only if its
    holdout log loss is lower than the default's, otherwise 0.9 is kept.
    Fit details are saved to kv `tail_shrink_fit_<coin>` for the app.
    """
    from statistics import NormalDist
    nd = NormalDist()
    windows = {}
    for ticker, ts, z, y in rows:
        try:
            ts, z = float(ts), float(z)
        except (TypeError, ValueError):
            continue
        if type(y) is not int or y not in (0, 1) or not math.isfinite(z) or not math.isfinite(ts):
            continue
        w = windows.setdefault(ticker, {"first": ts, "rows": []})
        w["first"] = min(w["first"], ts)
        w["rows"].append((z, y))
    ordered = [w["rows"] for w in sorted(windows.values(), key=lambda w: w["first"])]
    n_hold = max(MIN_HOLDOUT_WINDOWS, int(len(ordered) * HOLDOUT_FRACTION))
    details = {"ts": int(time.time() if now is None else now), "coin": coin,
               "windows": len(ordered), "value": tail_shrink(store, coin), "accepted": False}
    if len(ordered) - n_hold < MIN_FIT_WINDOWS:
        details["reason"] = (f"not enough windows: {len(ordered)} "
                             f"(need {MIN_FIT_WINDOWS + MIN_HOLDOUT_WINDOWS})")
        store.set_kv(f"tail_shrink_fit_{coin}", details)
        return details
    fit, hold = ordered[:-n_hold], ordered[-n_hold:]
    lo, hi = round(TAIL_SHRINK_MIN * 100), round(TAIL_SHRINK_MAX * 100)
    grid = [x / 100 for x in range(lo, hi + 1)]
    candidate = min(grid, key=lambda a: _window_logloss(a, fit, nd))
    ll_cand = _window_logloss(candidate, hold, nd)
    ll_default = _window_logloss(DEFAULT_TAIL_SHRINK, hold, nd)
    accepted = ll_cand < ll_default
    details.update({
        "candidate": candidate, "fit_windows": len(fit), "holdout_windows": len(hold),
        "holdout_logloss_candidate": round(ll_cand, 6),
        "holdout_logloss_default": round(ll_default, 6),
        "accepted": accepted, "value": candidate if accepted else DEFAULT_TAIL_SHRINK,
        "reason": "beat 0.9 on newer windows" if accepted else "did not beat 0.9 on newer windows",
    })
    store.set_kv(f"tail_shrink_{coin}", details["value"])
    store.set_kv(f"tail_shrink_fit_{coin}", details)
    return details


# --- tail model: a fitted Student-t shape + volatility scale -----------------------------
# The 0.9 shrink fattens tails everywhere, but settled 15-minute moves are
# PEAKED: thinner than normal at 1-2 sd (where 5-30c long shots live) and fatter
# only beyond ~2.5 sd (history.vol_check). A unit-variance Student-t has exactly
# that shape; vol_scale corrects a forecast that runs too wide or too narrow.
TAIL_DF_GRID = (3, 4, 5, 6, 8, 10, 15, 30)
VOL_SCALE_GRID = tuple(round(0.70 + 0.02 * i, 2) for i in range(21))   # 0.70 .. 1.10


def tail_model(store, coin):
    """Accepted (df, vol_scale) for this coin, or None to keep the normal + shrink."""
    m = store.get_kv(f"tail_model_{coin}") or {}
    try:
        df, scale = float(m["df"]), float(m["vol_scale"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (m.get("accepted") and math.isfinite(df) and min(TAIL_DF_GRID) <= df <= max(TAIL_DF_GRID)
            and math.isfinite(scale) and min(VOL_SCALE_GRID) <= scale <= max(VOL_SCALE_GRID)):
        return None
    return df, scale


def _t_loglik(zs, df, scale):
    """Mean log density of z when z / scale is unit-variance Student-t(df)."""
    from scipy.stats import t as student_t
    k = math.sqrt(df / (df - 2))
    x = np.asarray(zs) * k / scale
    return float(np.mean(student_t.logpdf(x, df) + math.log(k / scale)))


def _normal_loglik(zs, shrink):
    """Mean log density of z under today's pricing: Phi(shrink * z)."""
    x = np.asarray(zs) * shrink
    return float(np.mean(-0.5 * x * x - 0.5 * math.log(2 * math.pi) + math.log(shrink)))


def learn_tail_model(store, coin, samples, now=None):
    """samples: [(window_close_ts, z)], z = un-shrunk move in the forecast's sd
    (history.vol_check). Fit (df, vol_scale) on older windows; store it only if
    it beats today's pricing (normal with the current tail_shrink) on the newest
    held-out windows (HOLDOUT_FRACTION, at least MIN_HOLDOUT_WINDOWS). Details go
    to kv tail_model_<coin> either way, for the app."""
    now = int(time.time() if now is None else now)
    clean = sorted((int(w), float(z)) for w, z in samples
                   if z is not None and math.isfinite(float(z)))
    windows = sorted({w for w, _ in clean})
    n_hold = max(MIN_HOLDOUT_WINDOWS, int(len(windows) * HOLDOUT_FRACTION))
    current = tail_shrink(store, coin)
    details = {"ts": now, "coin": coin, "windows": len(windows), "accepted": False,
               "current_shrink": current}
    if len(windows) - n_hold < MIN_FIT_WINDOWS:
        details["reason"] = f"not enough windows: {len(windows)}"
        store.set_kv(f"tail_model_{coin}", details)
        return details
    cut = windows[-n_hold]
    fit = [z for w, z in clean if w < cut]
    hold = [z for w, z in clean if w >= cut]
    df, scale = max(((d, v) for d in TAIL_DF_GRID for v in VOL_SCALE_GRID),
                    key=lambda dv: _t_loglik(fit, *dv))
    ll_model, ll_current = _t_loglik(hold, df, scale), _normal_loglik(hold, current)
    accepted = ll_model > ll_current
    details.update({
        "df": df, "vol_scale": scale, "fit_windows": len(windows) - n_hold,
        "holdout_windows": n_hold, "holdout_loglik_model": round(ll_model, 5),
        "holdout_loglik_current": round(ll_current, 5), "accepted": accepted,
        "reason": ("beat current pricing on newer windows" if accepted
                   else "did not beat current pricing on newer windows"),
    })
    store.set_kv(f"tail_model_{coin}", details)
    return details


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
