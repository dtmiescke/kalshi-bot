"""An honest running scoreboard: learn from every settled window, scored before it learns.

Each settled 15-minute window is used twice, in time order. First the online model predicts it
from what was known at the first allowed decision (about 90 seconds in), without having seen the
result. Then it learns from the result. Comparing those predictions with Kalshi's own price on the
same windows gives a continuously updated out-of-sample score, with no train/test split to argue
about ("prequential" evaluation).

The model is a logistic regression that starts out equal to Kalshi's price (weight 1 on Kalshi's
log-odds, 0 on everything else) and moves away only as far as the results push it, with a pull back
toward that start. It places no bets. A second check asks whether the last 100 windows are doing
worse than the ones before them, so a market shift shows up as a drift alarm."""
import json
import math
import time

from . import entry_timing, ml

FEATURES = ("model_logit", "dist_sigma", "ret_5m", "ret_15m", "rsi_15m", "macd_hist", "adx_1m", "vol_ratio_15_60")
RECENT = 100
DRIFT_Z = -2.5          # recent windows this many standard errors worse than the earlier ones raise the alarm
MIN_WINDOWS = 30


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(z):
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def _loss(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _raw(feats):
    """The model's inputs for one decision, or None when the read has no Kalshi price."""
    if feats.get("kalshi_logit") is None:
        return None
    x = {"model_logit": _logit(feats["model_prob"]) if feats.get("model_prob") is not None else 0.0}
    for k in FEATURES[1:]:
        v = feats.get(k)
        x[k] = float(v) if v is not None else 0.0
    return x


class OnlineLogit:
    def __init__(self, lr=0.02, l2=0.01):
        self.lr, self.l2 = lr, l2
        self.kw, self.b = 1.0, 0.0                       # starts as Kalshi's own price
        self.w = {k: 0.0 for k in FEATURES}
        self.seen = {k: [0, 0.0, 0.0] for k in FEATURES}  # count, mean, M2 (Welford)

    def _x(self, raw):
        out = {}
        for k in FEATURES:
            n, mean, m2 = self.seen[k]
            sd = math.sqrt(m2 / (n - 1)) if n > 2 else 0.0
            out[k] = max(-4.0, min(4.0, (raw[k] - mean) / sd)) if sd > 1e-9 else 0.0
        return out

    def predict(self, feats):
        raw = _raw(feats)
        if raw is None:
            return None
        x = self._x(raw)
        z = self.kw * feats["kalshi_logit"] + self.b + sum(self.w[k] * x[k] for k in FEATURES)
        return _sigmoid(z)

    def learn(self, feats, y):
        raw = _raw(feats)
        if raw is None:
            return
        p = self.predict(feats)
        x = self._x(raw)
        err = p - y
        self.kw -= self.lr * (err * feats["kalshi_logit"] + self.l2 * (self.kw - 1.0))
        self.b -= self.lr * (err + self.l2 * self.b)
        for k in FEATURES:
            self.w[k] -= self.lr * (err * x[k] + self.l2 * self.w[k])
        for k in FEATURES:                                # update the running mean and spread after using them
            n, mean, m2 = self.seen[k]
            n += 1
            d = raw[k] - mean
            mean += d / n
            self.seen[k] = [n, mean, m2 + d * (raw[k] - mean)]


def decisions(store):
    """One settled window per ticker, at its earliest allowed decision, oldest first. Training rows from
    sources that disagree with outcomes far more than Kalshi's price does are left out."""
    ml.ensure_table(store)
    bad = ml.untrusted_sources(store)
    best = {}
    for r in store.q("SELECT ticker, ts, coin, features, y, source FROM ml_rows WHERE y IN (0,1)"):
        if (r["source"], r["coin"]) in bad:
            continue
        try:
            f = json.loads(r["features"])
        except ValueError:
            continue
        left = f.get("min_left")
        if left is None or f.get("kalshi_logit") is None or not 2 <= left <= 13.55:
            continue
        if r["ticker"] not in best or left > best[r["ticker"]][0]:
            best[r["ticker"]] = (left, r["ts"], f, r["y"])
    return sorted(((ts, f, y) for _, ts, f, y in best.values()), key=lambda r: r[0])


def _mean_se(xs):
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), None
    m = sum(xs) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in xs) / (n - 1))
    return m, sd / math.sqrt(n)


def run(rows, model=None):
    """Score `rows` [(ts, feats, y)] prequentially. Returns the per-window loss differences
    (Kalshi's loss minus the model's: positive means the model was better) and both loss lists."""
    model = model or OnlineLogit()
    diffs, lk, lo = [], [], []
    for _, f, y in rows:
        p_o = model.predict(f)
        if p_o is None:
            continue
        p_k = _sigmoid(f["kalshi_logit"])
        lk.append(_loss(p_k, y))
        lo.append(_loss(p_o, y))
        diffs.append(lk[-1] - lo[-1])
        model.learn(f, y)
    return diffs, lk, lo, model


def drift(diffs):
    """Are the last RECENT windows doing worse than the ones before them? diffs are oldest first."""
    if len(diffs) < 2 * RECENT:
        return None
    before, recent = diffs[:-RECENT], diffs[-RECENT:]
    mb, _ = _mean_se(before)
    mr, ser = _mean_se(recent)
    z = (mr - mb) / ser if ser else 0.0
    return {"windows": RECENT, "gain_per_window": round(mr, 5), "vs_before_z": round(z, 2), "alarm": bool(z <= DRIFT_Z)}


def bot_decisions(store):
    """The bot's own forecast against Kalshi's price, one settled window each, at its earliest allowed
    decision: [(ts, bot_prob, kalshi_price, outcome)], oldest first, from the decision audit."""
    best = {}
    for r in store.q("""SELECT ticker, ts, model_prob, market_price, outcome FROM decision_audit
                        WHERE decision='research' AND series LIKE 'KX%15M' AND outcome IN (0,1)
                        AND model_prob IS NOT NULL AND market_price IS NOT NULL"""):
        close = entry_timing.close_ts(r["ticker"])
        if close is None:
            continue
        left = (close - r["ts"]) / 60.0
        if not 2 <= left <= 13.55:
            continue
        if r["ticker"] not in best or left > best[r["ticker"]][0]:
            best[r["ticker"]] = (left, r["ts"], float(r["model_prob"]), float(r["market_price"]), int(r["outcome"]))
    return sorted(((ts, p, k, y) for _, ts, p, k, y in best.values()), key=lambda r: r[0])


def _summary(diffs, lk, lo):
    n = len(diffs)
    mean, se = _mean_se(diffs)
    return {"windows": n, "mine_logloss": round(sum(lo) / n, 4), "kalshi_logloss": round(sum(lk) / n, 4),
            "gain_per_window": round(mean, 5), "z": round(mean / se, 2) if se else None, "drift": drift(diffs)}


def report(store, now=None):
    """For the app. Safe on an empty table."""
    out = {"min_windows": MIN_WINDOWS, "ts": int(time.time() if now is None else now)}
    diffs, lk, lo, model = run(decisions(store))
    out["online"] = {"windows": len(diffs)}
    if len(diffs) >= MIN_WINDOWS:
        out["online"] = _summary(diffs, lk, lo)
        out["online"]["weights"] = {k: round(v, 3) for k, v in model.w.items()}
        out["online"]["kalshi_weight"] = round(model.kw, 3)
    bd = bot_decisions(store)
    lk2 = [_loss(k, y) for _, _, k, y in bd]
    lb = [_loss(p, y) for _, p, _, y in bd]
    out["bot"] = {"windows": len(bd)}
    if len(bd) >= MIN_WINDOWS:
        out["bot"] = _summary([a - b for a, b in zip(lk2, lb)], lk2, lb)
    return out
