"""Evidence-first meta research for the crypto/Kalshi strategy stack.

This module never places orders and never promotes a strategy from raw win rate.
It scores *evidence quality*, costs, out-of-sample stability, and drift so the
runner can learn what to distrust as well as what might be useful.
"""
from __future__ import annotations

from math import isfinite
from statistics import mean, median


def _finite(values):
    return [float(x) for x in values if x is not None and isfinite(float(x))]


def classify_regime(features):
    """Deterministic, explainable regime tags; no future candles are used."""
    f = features or {}
    vol = f.get("volatility_ratio")
    trend = f.get("trend_strength")
    divergence = f.get("source_divergence_bps")
    spread = f.get("spread_bps")
    imbalance = f.get("book_imbalance")
    tags = []
    if divergence is not None and float(divergence) >= 25:
        tags.append("feed_disagreement")
    if spread is not None and float(spread) >= 250:
        tags.append("wide_kalshi_spread")
    if vol is not None and float(vol) >= 2.0:
        tags.append("volatility_shock")
    if trend is not None and float(trend) >= 0.65:
        tags.append("trending")
    elif trend is not None and float(trend) <= 0.25:
        tags.append("ranging")
    if imbalance is not None and abs(float(imbalance)) >= 0.55:
        tags.append("orderbook_imbalance")
    return tags or ["mixed"]


def evidence_score(*, samples, out_of_sample_samples=0, net_edge=None,
                   profit_factor=None, max_drawdown=None, brier=None,
                   baseline_brier=None, drift=None, cost_coverage=False):
    """0..100 evidence-quality score; intentionally not a win-probability score."""
    n = max(0, int(samples or 0))
    oos = max(0, int(out_of_sample_samples or 0))
    score = min(20.0, 20.0 * n / 200.0) + min(25.0, 25.0 * oos / 100.0)
    if net_edge is not None and isfinite(float(net_edge)):
        score += 15.0 if float(net_edge) > 0 else 0.0
    if profit_factor is not None and isfinite(float(profit_factor)):
        score += 10.0 if float(profit_factor) > 1.15 else 0.0
    if max_drawdown is not None and isfinite(float(max_drawdown)):
        score += 10.0 if float(max_drawdown) < 0.10 else 0.0
    if brier is not None and baseline_brier is not None:
        if isfinite(float(brier)) and isfinite(float(baseline_brier)) and float(brier) < float(baseline_brier):
            score += 10.0
    if cost_coverage:
        score += 10.0
    if drift is not None and isfinite(float(drift)) and float(drift) > 0.5:
        score -= 20.0
    return round(max(0.0, min(100.0, score)), 1)


def walk_forward_summary(trades, min_train=30, min_test=20, cost_per_trade=0.0):
    """Chronological split; refuses tiny samples and subtracts costs from each trade.

    Each trade requires entry_ts and net_return (decimal, e.g. .01 = +1%).
    Input order is not trusted; timestamp order is enforced. No shuffle is used.
    """
    rows = []
    for t in trades or []:
        try:
            ts, ret = float(t["entry_ts"]), float(t["net_return"])
            if isfinite(ts) and isfinite(ret):
                rows.append((ts, ret))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])
    if len(rows) < int(min_train) + int(min_test):
        return {"eligible": False, "reason": "insufficient_chronological_samples",
                "samples": len(rows), "train_samples": 0, "test_samples": 0}
    split = int(len(rows) * 0.7)
    train = rows[:split]
    test = rows[split:]
    if len(train) < min_train or len(test) < min_test:
        return {"eligible": False, "reason": "insufficient_train_or_test_samples",
                "samples": len(rows), "train_samples": len(train), "test_samples": len(test)}
    net = [r - max(0.0, float(cost_per_trade)) for _, r in test]
    wins = [r for r in net if r > 0]
    losses = [-r for r in net if r < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    equity = peak = 1.0
    max_dd = 0.0
    for r in net:
        equity *= max(0.0, 1.0 + r)
        peak = max(peak, equity)
        if peak:
            max_dd = max(max_dd, (peak - equity) / peak)
    pf = gross_win / gross_loss if gross_loss else ("infinite_no_losses" if gross_win else 0.0)
    return {
        "eligible": True, "samples": len(rows), "train_samples": len(train),
        "test_samples": len(test), "test_win_rate": round(len(wins) / len(net), 4),
        "test_mean_net_return": round(mean(net), 8), "test_median_net_return": round(median(net), 8),
        "test_profit_factor": round(pf, 4) if isinstance(pf, float) and isfinite(pf) else pf,
        "test_max_drawdown": round(max_dd, 6),
        "cost_per_trade": max(0.0, float(cost_per_trade)),
        "note": "Chronological holdout only; test data must not be used to tune thresholds."
    }


def detect_feature_drift(reference, recent, threshold=0.5):
    """Robust median/MAD shift diagnostic for same-unit feature samples."""
    a, b = _finite(reference), _finite(recent)
    if len(a) < 20 or len(b) < 10:
        return {"drift_score": None, "drift_detected": False,
                "reason": "insufficient_reference_or_recent_history"}
    center = median(a)
    mad = median([abs(x - center) for x in a])
    scale = max(1.4826 * mad, 1e-9)
    shift = abs(median(b) - center) / scale
    return {"drift_score": round(shift, 4), "drift_detected": shift >= float(threshold),
            "reference_samples": len(a), "recent_samples": len(b),
            "reference_median": round(center, 8), "recent_median": round(median(b), 8),
            "threshold": float(threshold),
            "note": "Drift is a review trigger, not a directional trading signal."}
