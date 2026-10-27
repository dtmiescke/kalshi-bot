"""The learning brain.

Blend: each signal's probability is converted to log-odds and averaged, weighted
by (learned weight x the reading's confidence). Log-odds pooling treats a move
from 90% to 95% as the big deal it is, unlike plain averaging.

Learn: after every market settles, each signal is scored with log loss against
the result, relative to the Kalshi price. Signals that beat the market gain
weight; ones that don't, lose it (multiplicative weights / Hedge). Every change
is appended to history, never overwritten, so nothing learned is ever lost.

Calibrate: the bot also checks itself — when it says 70%, does it win ~70%?
Once there's enough data, it corrects its own over- or under-confidence.
"""
import math
from collections import defaultdict

from .signals.base import clamp, logit, sigmoid

DEFAULT_WEIGHTS = {
    "kalshi_market": 1.0,      # the anchor
    "book_consensus": 1.5,     # sharp books are the strongest known signal
    "line_movement": 0.4,
    "reverse_line_movement": 0.4,
    "trade_flow": 0.3,
    "ai_news": 0.3,
    "schedule_spot": 0.3,
    "crypto_model": 1.2,       # spot vs strike vs volatility
    "crypto_momentum": 0.2,
    "crypto_orderbook": 0.5,  # cross-venue L2; learned from settled outcomes
    "crypto_model_iv": 0.8,    # same model on option-implied (Deribit) volatility
    "spot_flow": 0.2,
    "ml_model": 2.0,           # trained model; only exists after passing out-of-sample tests
    # New strategy signals start small and must earn weight through settled outcomes.
    "crypto_trend_strategy": 0.15,
    "crypto_mean_reversion_strategy": 0.12,
    "crypto_breakout_strategy": 0.10,
    "crypto_multi_timeframe_chart": 0.15,
    "crypto_perp_funding": 0.10,   # Hyperliquid crowding (contrarian)
    "crypto_perp_premium": 0.10,   # Hyperliquid perp-vs-oracle flow
}
MIN_W, MAX_W = 0.05, 5.0
ETA = 0.05            # learning rate: small = slow, steady learning
CAL_MIN_SAMPLES = 60  # don't trust calibration until this many graded markets
MIN_LEARN_SAMPLES = 20  # per signal and domain; prevents single-result overfitting


def domain_of(ticker_or_series):
    """Broad calibration domain: sports vs short-window crypto."""
    t = (ticker_or_series or "").upper()
    return "crypto" if t.startswith(("KXBTC", "KXETH", "KXSOL", "KXBNB")) or "15M" in t else "sports"


def learning_domain_of(ticker_or_series):
    """Return a conservative league-level learning bucket when identifiable.

    Keep calibration and risk compatibility on the broad sports/crypto domains,
    but let each league learn its own signal weights. Unknown series stay in the
    shared sports bucket until there is enough evidence to classify them.
    """
    t = str(ticker_or_series or "").upper().replace("-", "_")
    if domain_of(t) == "crypto":
        return "crypto"
    # Match specific league tokens, not generic sport words, to avoid accidental
    # cross-sport contamination. Prefix variants are common across Kalshi series.
    groups = (
        ("nfl", ("KXNFL", "NFLGAME", "AMERICANFOOTBALL_NFL")),
        ("college_football", ("NCAAF", "COLLEGE_FOOTBALL", "KXCFB")),
        ("nba", ("KXNBA", "NBAGAME", "BASKETBALL_NBA")),
        ("wnba", ("KXWNBA", "WNBAGAME", "BASKETBALL_WNBA")),
        ("college_basketball", ("NCAAB", "COLLEGE_BASKETBALL", "BASKETBALL_NCAAB")),
        ("mlb", ("KXMLB", "MLBGAME", "BASEBALL_MLB")),
        ("nhl", ("KXNHL", "NHLGAME", "ICEHOCKEY_NHL")),
        ("tennis", ("TENNIS", "ATP", "WTA")),
        ("mma", ("MMA", "UFC")),
        ("boxing", ("BOXING",)),
        ("golf", ("GOLF", "PGA", "LPGA")),
        ("motorsports", ("F1", "NASCAR", "MOTORSPORT", "RACING")),
        ("soccer", ("SOCCER", "EPL", "MLS", "UEFA", "LALIGA", "SERIEA", "BUNDESLIGA")),
        ("cricket", ("CRICKET", "IPL")),
        ("rugby", ("RUGBY",)),
        ("esports", ("ESPORT", "LEAGUEOFLEGENDS", "DOTA", "COUNTERSTRIKE", "CS2")),
    )
    for domain, tokens in groups:
        if any(token in t for token in tokens):
            return domain
    return "sports"


def decision_cutoff(ticker, start):
    """The moment a signal should be judged at.

    Sports: the last reading before kick-off (the closing line).
    Crypto 15-minute: NOT the last second before the bell (by then everything,
    including Kalshi, already knows the answer, which would teach the brain
    nothing about real decisions) but the last reading at least 2 minutes before
    close — the end of the window in which the bot actually trades."""
    return start - 120 if domain_of(ticker) == "crypto" else start


def get_weights(store, domain=None):
    """Merge defaults, shared domain weights, then the most specific league weights.

    A league inherits broad sports learning until it has its own evidence, then
    its own learned values override the shared baseline signal by signal.
    """
    w = dict(DEFAULT_WEIGHTS)
    learned = store.current_weights()
    w.update({k: v for k, v in learned.items() if "@" not in k})
    if domain:
        broad = "crypto" if domain == "crypto" else "sports"
        for bucket in (broad, domain):
            suffix = "@" + bucket
            w.update({k[:-len(suffix)]: v for k, v in learned.items() if k.endswith(suffix)})
    return w


def blend(readings, weights):
    """Return (prob, external_confidence) from a list of Readings."""
    num = den = 0.0
    ext_conf_num = ext_conf_den = 0.0
    for r in readings:
        w = weights.get(r.name, 0.3) * r.confidence
        num += w * logit(r.prob)
        den += w
        if r.name != "kalshi_market":
            ext_conf_num += w * r.confidence
            ext_conf_den += w
    if den == 0:
        return None, 0.0
    ext_conf = ext_conf_num / ext_conf_den if ext_conf_den else 0.0
    blended = num / den
    # when the independent outside signals argue with each other, trust the
    # blend less. trade_flow is built around the Kalshi price itself, so its
    # gap from a model is the edge being measured, not disagreement.
    ext = [(weights.get(r.name, 0.3) * r.confidence, logit(r.prob)) for r in readings
           if r.name not in ("kalshi_market", "trade_flow")]
    wsum = sum(w for w, _ in ext)
    if len(ext) >= 2 and wsum > 0:
        mean = sum(w * x for w, x in ext) / wsum
        sd = (sum(w * (x - mean) ** 2 for w, x in ext) / wsum) ** 0.5
        ext_conf *= max(0.5, 1 - sd / 3)   # sd of 1.5 logits = heavy disagreement
    return clamp(sigmoid(blended)), ext_conf


def log_loss(p, y):
    p = clamp(p, 1e-4, 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def learn(store, graded, domain=None):
    """graded: list of {signal: prob, ..., '_outcome': 0/1}, one per settled market.

    Each signal is judged against the Kalshi price on the SAME market, so a
    signal only gains weight by being better than the market, not by luck of
    which markets it happened to show up on.
    """
    weights = get_weights(store, domain)
    totals = defaultdict(float)
    counts = defaultdict(int)
    for g in graded:
        y = g["_outcome"]
        mkt = g.get("kalshi_market")
        if mkt is None:
            continue
        base = log_loss(mkt, y)
        for name, p in g.items():
            if name.startswith("_") or name == "kalshi_market" or p is None:
                continue
            totals[name] += log_loss(p, y) - base   # negative = beat the market
            counts[name] += 1
    changes = {}
    for name, rel_total in totals.items():
        n = counts[name]
        # League-specific samples are sparse at first. Do not let a handful of
        # lucky/unlucky outcomes swing weights; normalize loss by sample count so
        # the learning rate reflects average evidence, not how many rows arrived.
        if n < MIN_LEARN_SAMPLES:
            continue
        rel = rel_total / n
        old = weights.get(name, 0.3)
        new = clamp(old * math.exp(-ETA * rel), MIN_W, MAX_W)
        if abs(new - old) > 1e-6:
            store.set_weight(f"{name}@{domain}" if domain else name, new,
                             f"{n} markets, mean_rel_logloss={rel:+.4f}")
            changes[name] = (old, new)
    return changes


# --- calibration ---------------------------------------------------------------
def calibration_table(store, buckets=10, domain=None, as_of_ts=None):
    """Calibrate YES probabilities using settled, canonical forecasts available at a given time.

    Decision-audit rows are scored only at the canonical pre-event cutoff, so
    rejected opportunities contribute without counting repeated scans as new samples.
    as_of_ts enables leak-free walk-forward evaluation; older installations fall
    back to settled bets only when no scored audit rows exist for that domain/time.
    """
    rows = store.q("""SELECT model_prob, outcome, series FROM decision_audit
                      WHERE brier IS NOT NULL AND model_prob IS NOT NULL
                      AND outcome IN (0,1) AND (? IS NULL OR settled_ts<=?)""",
                   (as_of_ts, as_of_ts))
    if domain:
        rows = [r for r in rows if domain_of(r["series"]) == domain]
    audit_source = bool(rows)
    if not rows:
        rows = store.q("""SELECT model_prob, result, series FROM bets
                          WHERE status='graded' AND result IN ('yes','no')
                          AND model_prob IS NOT NULL AND (? IS NULL OR graded_ts<=?)""",
                       (as_of_ts, as_of_ts))
        if domain:
            rows = [r for r in rows if domain_of(r["series"]) == domain]
    table = defaultdict(lambda: [0, 0])
    for r in rows:
        p = float(r["model_prob"])
        outcome = (int(r["outcome"]) if audit_source
                   else 1 if r["result"] == "yes" else 0)
        b = min(int(p * buckets), buckets - 1)
        table[b][0] += 1
        table[b][1] += outcome
    return {b: {"n": n, "predicted": (b + 0.5) / buckets, "actual": w / n}
            for b, (n, w) in sorted(table.items())}


def calibrate(prob, store, domain=None, as_of_ts=None):
    """Shrink or stretch a probability using only prior settled forecasts."""
    return calibrate_with_table(prob, calibration_table(store, domain=domain, as_of_ts=as_of_ts))


def calibrate_with_table(prob, table):
    """calibrate() with a precomputed calibration_table (backtests reuse tables)."""
    n_total = sum(v["n"] for v in table.values())
    if n_total < CAL_MIN_SAMPLES:
        return prob
    b = min(int(prob * 10), 9)
    cell = table.get(b)
    if not cell or cell["n"] < 10:
        return prob
    # move partway toward observed rate, more when the bucket is well sampled
    trust = min(cell["n"] / 100, 0.5)
    return clamp(prob + trust * (cell["actual"] - cell["predicted"]))
