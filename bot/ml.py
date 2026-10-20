"""The trained brain for 15-minute crypto: learns WHEN KALSHI IS WRONG.

Data: every replayed history minute and every live decision is stored as a
row of features (features.py) with the window's real result once it settles.

Training (free: scikit-learn gradient boosting):
- Split strictly by TIME: oldest 70% train, next 15% validate, newest 15% test.
  Windows never straddle splits, so the model can't peek at the future.
- Each window counts equally (minutes inside one window are near-duplicates).
- Number of trees is chosen on the validation set.
- A STACKER (logistic regression, fitted on the validation set) then blends
  three views: the boosted trees, the exact-contract formula, and Kalshi's own
  price. With little data it leans on the formula; as data grows it trusts the
  trees more. Its output is calibrated, so "70%" really means ~70%.
- It's judged on the untouched test months against (a) Kalshi's own price and
  (b) the current champion model. It's promoted ONLY if it beats both, and the
  simulated trades on the test set made money after fees.
Every training run and its scores are logged; old champions are kept.
"""
import json
import logging
import math
import os
import time

import numpy as np

from . import features

log = logging.getLogger("bot")
MODEL_DIR = os.environ.get("MODEL_DIR", "data/models")
COINS = ("BTC", "ETH", "SOL")


def ensure_table(store):
    store.x("""CREATE TABLE IF NOT EXISTS ml_rows (
        ticker TEXT, ts INTEGER, coin TEXT, version INTEGER, features TEXT,
        y INTEGER, source TEXT, PRIMARY KEY (ticker, ts))""")
    store.x("""CREATE TABLE IF NOT EXISTS model_runs (
        ts INTEGER, name TEXT, promoted INTEGER, metrics TEXT)""")


def add_row(store, ticker, ts, coin, feats, y=None, source="live"):
    ensure_table(store)
    store.x("INSERT OR IGNORE INTO ml_rows VALUES(?,?,?,?,?,?,?)",
            (ticker, int(ts), coin, features.FEATURE_VERSION, json.dumps(feats), y, source))


def label(store, ticker, y):
    ensure_table(store)
    store.x("UPDATE ml_rows SET y=? WHERE ticker=? AND y IS NULL", (int(y), ticker))


def coin_of(ticker):
    t = ticker.upper()
    return next((c for c in COINS if t.startswith(f"KX{c}")), "BTC")


# --- dataset --------------------------------------------------------------------------
def load_dataset(store):
    ensure_table(store)
    rows = store.q("""SELECT ticker, ts, coin, features, y FROM ml_rows
                      WHERE y IS NOT NULL AND version=? ORDER BY ts""", (features.FEATURE_VERSION,))
    names = features.names()
    X, y, groups, ts, coins = [], [], [], [], []
    for r in rows:
        f = json.loads(r["features"])
        if "kalshi_logit" not in f:
            continue
        X.append([f.get(n, 0.0) for n in names] + [1.0 if r["coin"] == c else 0.0 for c in COINS])
        y.append(r["y"])
        groups.append(r["ticker"])
        ts.append(r["ts"])
        coins.append(r["coin"])
    cols = names + [f"coin_{c}" for c in COINS]
    return np.array(X, dtype=float), np.array(y, dtype=int), np.array(groups), np.array(ts), cols


def paired_bootstrap(p_model, p_base, y, groups, n=1000, seed=7):
    """Share of resamples (by WINDOW) in which the model beats the baseline.
    A model that only wins on one lucky test set scores near 0.5, not ~1.0."""
    rng = np.random.default_rng(seed)
    pm, pb = np.clip(p_model, 1e-4, 1 - 1e-4), np.clip(p_base, 1e-4, 1 - 1e-4)
    gain = -(y * np.log(pb) + (1 - y) * np.log(1 - pb)) + (y * np.log(pm) + (1 - y) * np.log(1 - pm))
    uniq, inv = np.unique(groups, return_inverse=True)
    per = np.bincount(inv, weights=gain) / np.bincount(inv)
    wins = 0
    for _ in range(n):
        wins += per[rng.integers(0, len(per), len(per))].mean() > 0
    return wins / n


def time_split(groups, ts, fracs=(0.70, 0.15)):
    """Split WINDOWS (not rows) by their time order."""
    first = {}
    for g, t in zip(groups, ts):
        first[g] = min(first.get(g, t), t)
    order = sorted(first, key=first.get)
    n = len(order)
    a, b = int(n * fracs[0]), int(n * (fracs[0] + fracs[1]))
    sets = [set(order[:a]), set(order[a:b]), set(order[b:])]
    return [np.array([g in s for g in groups]) for s in sets]


def window_weights(groups):
    _, inv, counts = np.unique(groups, return_inverse=True, return_counts=True)
    return 1.0 / counts[inv]


def logloss(p, y, w):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-(w * (y * np.log(p) + (1 - y) * np.log(1 - p))).sum() / w.sum())


SLIPPAGE = float(os.environ.get("SIM_SLIPPAGE", 0.005))   # $ per contract
MAX_SIM_SPREAD = 0.08


def sim_trades(p, X, cols, y, groups, fee_rate=0.07, thresholds=(0.03, 0.05, 0.08)):
    """First qualifying minute per window, buy the better side at the ask.
    REALISM: pays half a cent of slippage on top of the ask, and skips minutes
    where the spread was so wide the quoted ask likely had no real size."""
    li, si = cols.index("kalshi_logit"), cols.index("kalshi_spread")
    out = {}
    for t in thresholds:
        done, pnl, staked, wins = set(), 0.0, 0.0, 0
        for i in range(len(p)):
            if groups[i] in done:
                continue
            mid = 1 / (1 + math.exp(-X[i, li]))
            sp = X[i, si]
            if sp > MAX_SIM_SPREAD:
                continue
            for side, pw, cost in (("yes", p[i], mid + sp / 2 + SLIPPAGE),
                                   ("no", 1 - p[i], 1 - mid + sp / 2 + SLIPPAGE)):
                if not 0.05 <= cost <= 0.95:
                    continue
                fee = math.ceil(fee_rate * cost * (1 - cost) * 100 * 100 - 1e-9) / 100 / 100
                if pw - cost - fee >= t:
                    won = (y[i] == 1) == (side == "yes")
                    pnl += (1 - cost if won else -cost) - fee
                    staked += cost
                    wins += won
                    done.add(groups[i])
                    break
        n = len(done)
        out[t] = {"trades": n, "win_rate": round(wins / n, 3) if n else None,
                  "roi": round(pnl / staked, 4) if staked else None}
    return out


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def stack_inputs(gbm, X, li, mi):
    return np.column_stack([_logit(gbm.predict_proba(X)[:, 1]), _logit(X[:, mi]), X[:, li]])


class Stacker:
    """Blends trees + formula + Kalshi price with weights learned on validation data."""
    def __init__(self, lr, li, mi):
        self.lr, self.li, self.mi = lr, li, mi

    def predict_with(self, gbm, X):
        return np.clip(self.lr.predict_proba(stack_inputs(gbm, X, self.li, self.mi))[:, 1], 0.01, 0.99)

    def weights(self):
        return dict(zip(("trees", "formula", "kalshi"), [round(float(x), 3) for x in self.lr.coef_[0]]))


# --- training ---------------------------------------------------------------------------
def learn_tails(store):
    """Fit each coin's fat-tail adjustment from labelled rows."""
    from . import vol as volmod
    ensure_table(store)
    out = {}
    for coin in COINS:
        rows = store.q("SELECT features, y FROM ml_rows WHERE coin=? AND y IS NOT NULL", (coin,))
        pairs = [(json.loads(r["features"]).get("model_prob", 0.5), r["y"]) for r in rows]
        v = volmod.learn_tail_shrink(store, coin, pairs)
        if v:
            out[coin] = v
    return out


def train(store, min_windows=300, fee_rate=0.07):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.inspection import permutation_importance
    import joblib

    tails = learn_tails(store)
    X, y, groups, ts, cols = load_dataset(store)
    n_windows = len(set(groups.tolist())) if len(groups) else 0
    if n_windows < min_windows:
        return {"status": "not enough data", "windows": n_windows, "needed": min_windows}
    tr, va, te = time_split(groups, ts)
    w = window_weights(groups)
    li = cols.index("kalshi_logit")
    kalshi_p = 1 / (1 + np.exp(-X[:, li]))

    best, best_ll = None, 1e9
    for iters in (100, 200, 400):
        m = HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, max_iter=iters,
                                           l2_regularization=1.0, min_samples_leaf=40,
                                           early_stopping=False, random_state=7)
        m.fit(X[tr], y[tr], sample_weight=w[tr])
        ll = logloss(m.predict_proba(X[va])[:, 1], y[va], w[va])
        if ll < best_ll:
            best, best_ll = m, ll
    mi = cols.index("model_prob")
    stack = LogisticRegression(C=1.0)
    stack.fit(stack_inputs(best, X[va], li, mi), y[va], sample_weight=w[va])
    iso = Stacker(stack, li, mi)            # name kept for saved-file compatibility
    p_test = iso.predict_with(best, X[te])

    metrics = {
        "windows": n_windows, "rows": int(len(y)),
        "test_windows": int(len(set(groups[te].tolist()))),
        "test_logloss_model": round(logloss(p_test, y[te], w[te]), 5),
        "test_logloss_kalshi": round(logloss(kalshi_p[te], y[te], w[te]), 5),
        "test_logloss_formula": round(logloss(X[te, cols.index("model_prob")], y[te], w[te]), 5),
        "test_trades": sim_trades(p_test, X[te], cols, y[te], groups[te], fee_rate),
        "stack_weights": iso.weights(), "tail_shrink": tails,
    }
    # which tools actually matter (on unseen test data)
    sub = np.where(te)[0][:6000]
    try:
        imp = permutation_importance(best, X[sub], y[sub], n_repeats=3, random_state=7,
                                     scoring="neg_log_loss")
        ranked = sorted(zip(cols, imp.importances_mean), key=lambda x: -x[1])
        metrics["top_features"] = [[c, round(float(v), 5)] for c, v in ranked[:15]]
    except Exception as e:
        metrics["top_features"] = [["error", str(e)]]

    champ = load_meta()
    champ_ll = None
    if champ and os.path.exists(os.path.join(MODEL_DIR, "crypto_model.joblib")):
        try:
            cm = joblib.load(os.path.join(MODEL_DIR, "crypto_model.joblib"))
            champ_ll = logloss(cm["iso"].predict_with(cm["model"], X[te]), y[te], w[te])
        except Exception:
            champ_ll = None
    metrics["test_logloss_champion"] = round(champ_ll, 5) if champ_ll else None

    # PROMOTION GATES — every one must pass:
    # 1. better than Kalshi's own price on unseen windows...
    # 2. ...and not by luck: wins in >= 90% of bootstrap resamples of those windows
    # 3. better than the current champion
    # 4. enough unseen windows to judge at all
    # 5. simulated trades on unseen data made money (>= 30 trades)
    conf = paired_bootstrap(p_test, kalshi_p[te], y[te], groups[te])
    min_test = int(os.environ.get("ML_MIN_TEST_WINDOWS", 100))
    beats_kalshi = metrics["test_logloss_model"] < metrics["test_logloss_kalshi"] * 0.998
    beats_champ = champ_ll is None or metrics["test_logloss_model"] < champ_ll
    profitable = [t for t, v in metrics["test_trades"].items()
                  if v["trades"] >= 30 and (v["roi"] or 0) > 0]
    enough = metrics["test_windows"] >= min_test
    promoted = beats_kalshi and conf >= 0.90 and beats_champ and bool(profitable) and enough
    metrics.update({"beats_kalshi": beats_kalshi, "bootstrap_confidence": round(conf, 3),
                    "beats_champion": beats_champ, "enough_test_windows": enough,
                    "profitable_thresholds": profitable, "promoted": promoted})
    if promoted:
        os.makedirs(MODEL_DIR, exist_ok=True)
        if champ:   # keep the old champion
            stamp = int(time.time())
            for ext in ("joblib", "json"):
                src = os.path.join(MODEL_DIR, f"crypto_model.{ext}")
                if os.path.exists(src):
                    os.replace(src, os.path.join(MODEL_DIR, f"crypto_model_{stamp}.{ext}"))
        joblib.dump({"model": best, "iso": iso, "cols": cols}, os.path.join(MODEL_DIR, "crypto_model.joblib"))
        meta = {"trained": time.time(), "feature_version": features.FEATURE_VERSION,
                "cols": cols, "min_edge": min(profitable), **metrics}
        with open(os.path.join(MODEL_DIR, "crypto_model.json"), "w") as f:
            json.dump(meta, f, indent=1, default=str)
        _CACHE.clear()
    ensure_table(store)
    store.x("INSERT INTO model_runs VALUES(?,?,?,?)",
            (int(time.time()), "crypto_model", int(promoted), json.dumps(metrics, default=str)))
    return metrics


# --- live prediction ---------------------------------------------------------------------
_CACHE = {}


def load_meta():
    p = os.path.join(MODEL_DIR, "crypto_model.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def predict(feats, coin):
    """Calibrated P(YES) from the champion model, or None if there isn't one."""
    if "m" not in _CACHE:
        path = os.path.join(MODEL_DIR, "crypto_model.joblib")
        meta = load_meta()
        if not (meta and os.path.exists(path) and meta.get("feature_version") == features.FEATURE_VERSION):
            _CACHE["m"] = None
        else:
            import joblib
            _CACHE["m"] = (joblib.load(path), meta)
    if not _CACHE["m"] or "kalshi_logit" not in feats:
        return None, None
    bundle, meta = _CACHE["m"]
    row = [feats.get(c, 0.0) for c in bundle["cols"] if not c.startswith("coin_")] + \
          [1.0 if coin == c else 0.0 for c in COINS]
    p = float(bundle["iso"].predict_with(bundle["model"], np.array([row]))[0])
    return p, meta


def text(m):
    if m.get("status"):
        return f"Model training skipped: {m['status']} ({m.get('windows')} windows, need {m.get('needed')})."
    lines = ["=== MODEL TRAINING (walk-forward, newest 15% held out) ===",
             f"{m['windows']} windows, {m['rows']} rows, tested on {m['test_windows']} unseen windows",
             f"Log loss (lower = better): model {m['test_logloss_model']} | Kalshi price "
             f"{m['test_logloss_kalshi']} | formula {m['test_logloss_formula']} | champion "
             f"{m['test_logloss_champion']}"]
    for t, v in m["test_trades"].items():
        lines.append(f"  edge >= {float(t):.0%}: {v['trades']} trades, win "
                     f"{(v['win_rate'] or 0):.0%}, return {(v['roi'] or 0):+.1%}")
    lines.append("Most useful tools: " + ", ".join(f[0] for f in m.get("top_features", [])[:10]))
    lines.append(f"Confidence it really beats Kalshi (bootstrap): {m.get('bootstrap_confidence', 0):.0%}")
    lines.append("PROMOTED to live trading." if m["promoted"] else
                 "Not promoted (needs: beat Kalshi with >=90% bootstrap confidence, beat the "
                 "champion, >=100 unseen windows, profitable simulated trades).")
    return "\n".join(lines)
