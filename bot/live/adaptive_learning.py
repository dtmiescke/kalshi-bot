"""Online, regime-aware strategy evaluation with conservative promotion gates.

This module learns signal reliability from resolved outcomes. It does not place
orders, modify risk limits, or claim that a score is a calibrated probability.
Persist state externally and version it; only train on resolved observations.
"""
from __future__ import annotations
from collections import defaultdict, deque
from math import isfinite
from statistics import mean


class AdaptiveLearning:
    def __init__(self, window=500, min_samples=40, min_edge=0.02):
        self.window = max(20, int(window))
        self.min_samples = max(10, int(min_samples))
        self.min_edge = max(0.0, float(min_edge))
        self.outcomes = defaultdict(lambda: deque(maxlen=self.window))
        self.regime_outcomes = defaultdict(lambda: deque(maxlen=self.window))
        self.pending = {}

    @staticmethod
    def regime(features):
        """Conservative, explainable regime tags from observed market features."""
        f = features or {}
        try:
            vol = float(f.get("realized_volatility", 0) or 0)
            spread = float(f.get("spread_bps", 0) or 0)
            divergence = float(f.get("source_divergence_bps", 0) or 0)
            imbalance = abs(float(f.get("book_imbalance", 0) or 0))
            if not all(isfinite(x) for x in (vol, spread, divergence, imbalance)):
                return "uncertain"
            if spread >= 40 or divergence >= 35:
                return "dislocated"
            if vol >= 0.025:
                return "high_volatility"
            if spread >= 20:
                return "thin_liquidity"
            if imbalance >= 0.55:
                return "order_flow_imbalanced"
            trend = float(f.get("trend_strength", 0) or 0)
            if abs(trend) >= 0.65:
                return "trending"
            if abs(trend) <= 0.15 and vol <= 0.008:
                return "ranging"
            return "mixed"
        except (TypeError, ValueError):
            return "uncertain"

    def record_prediction(self, prediction_id, strategy, regime, side, probability,
                          market_probability, cost_probability, metadata=None):
        """Store an immutable prediction snapshot; duplicate IDs are rejected."""
        if prediction_id in self.pending:
            raise ValueError("prediction_id already exists")
        p, mp, cp = float(probability), float(market_probability), float(cost_probability)
        if not all(isfinite(x) and 0 <= x <= 1 for x in (p, mp, cp)):
            raise ValueError("probabilities must be finite and between zero and one")
        edge = (p - cp) if side == "yes" else ((1 - p) - (1 - cp))
        self.pending[prediction_id] = {
            "strategy": str(strategy), "regime": str(regime), "side": str(side),
            "probability": p, "market_probability": mp, "cost_probability": cp,
            "edge_at_entry": edge, "metadata": metadata or {},
        }
        return dict(self.pending[prediction_id])

    def resolve(self, prediction_id, outcome_yes):
        """Learn only after a real resolved outcome is supplied."""
        if prediction_id not in self.pending:
            raise KeyError("unknown prediction_id")
        row = self.pending.pop(prediction_id)
        y = 1.0 if bool(outcome_yes) else 0.0
        correct = (y == 1.0) if row["side"] == "yes" else (y == 0.0)
        sample = {"correct": float(correct), "brier": (row["probability"] - y) ** 2,
                  "edge_at_entry": row["edge_at_entry"]}
        self.outcomes[row["strategy"]].append(sample)
        self.regime_outcomes[(row["strategy"], row["regime"])].append(sample)
        return {"strategy": row["strategy"], "regime": row["regime"],
                "correct": correct, "brier": sample["brier"]}

    def report(self, strategy, regime=None):
        samples = self.regime_outcomes[(strategy, regime)] if regime is not None else self.outcomes[strategy]
        rows = list(samples)
        n = len(rows)
        return {
            "strategy": strategy, "regime": regime, "samples": n,
            "win_rate": mean(x["correct"] for x in rows) if rows else None,
            "mean_brier": mean(x["brier"] for x in rows) if rows else None,
            "mean_entry_edge": mean(x["edge_at_entry"] for x in rows) if rows else None,
            "eligible_for_promotion": n >= self.min_samples and
                mean(x["edge_at_entry"] for x in rows) >= self.min_edge and
                mean(x["brier"] for x in rows) < 0.20,
            "promotion_note": "Eligibility is a research gate, not proof of profitability; require walk-forward, cost and drawdown checks."
        }

    def candidate_weights(self, strategies, regime):
        """Weights are normalized from evidence-backed candidate scores; unseen strategies get zero."""
        raw = {}
        for strategy in strategies:
            report = self.report(strategy, regime)
            if report["eligible_for_promotion"]:
                raw[strategy] = max(0.0, report["mean_entry_edge"]) * report["win_rate"]
        total = sum(raw.values())
        return {name: score / total for name, score in raw.items()} if total else {}


def graded_setup_performance(store, min_samples=20, limit=5000):
    """Aggregate only canonical, settled decision-audit rows by setup and regime.

    The market-price Brier score is a baseline; no setup is promoted from a raw
    win rate alone. Returns a JSON-friendly report and does not mutate weights.
    """
    rows = store.q(
        "SELECT model_prob, market_price, outcome, signals_json, settled_ts "
        "FROM decision_audit WHERE outcome IS NOT NULL AND brier IS NOT NULL "
        "ORDER BY settled_ts DESC LIMIT ?", (int(limit),)
    )
    buckets = defaultdict(list)
    for row in rows:
        try:
            payload = row.get("signals_json") or "{}"
            data = __import__("json").loads(payload) if isinstance(payload, str) else payload
            setups = data.get("chart_setups") or []
            names = {str(x) for x in setups if x}
            regime = str(data.get("regime") or "mixed")
            p, y = float(row["model_prob"]), float(row["outcome"])
            market = row.get("market_price")
            if not isfinite(p) or not 0 <= p <= 1 or y not in (0.0, 1.0):
                continue
            model_brier = (p-y)**2
            market_brier = (float(market)-y)**2 if market is not None and isfinite(float(market)) and 0 <= float(market) <= 1 else None
            for name in names:
                buckets[(name, "all")].append((model_brier, market_brier))
                buckets[(name, regime)].append((model_brier, market_brier))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    report = {}
    for (name, regime), samples in buckets.items():
        n = len(samples)
        model_score = mean(x[0] for x in samples)
        baseline = [x[1] for x in samples if x[1] is not None]
        market_score = mean(baseline) if baseline else None
        report[f"{name}@{regime}"] = {
            "setup": name, "regime": regime, "samples": n,
            "model_brier": round(model_score, 6),
            "market_brier": round(market_score, 6) if market_score is not None else None,
            "beats_market_brier": bool(market_score is not None and model_score < market_score),
            "eligible_for_adaptation": bool(n >= int(min_samples) and market_score is not None),
        }
    return report


def historical_setup_penalty(store, setup_names, regime="mixed", min_samples=20, report=None):
    """Return a bounded extra required edge only for adequately sampled weak setups.

    The maximum surcharge is 1.5 percentage points. Positive historical evidence
    never lowers the existing required edge; it must be promoted through separate
    out-of-sample evaluation. This prevents a lucky in-sample streak from
    automatically increasing exposure.
    """
    report = report if report is not None else graded_setup_performance(store, min_samples=min_samples)
    penalty = 0.0
    evidence = []
    for name in set(str(x) for x in (setup_names or [])):
        row = report.get(f"{name}@{regime}") or report.get(f"{name}@all")
        if not row or not row["eligible_for_adaptation"]:
            continue
        baseline = row["market_brier"]
        if baseline is None:
            continue
        gap = row["model_brier"] - baseline
        if gap > 0:
            penalty += min(0.005, gap * 0.05)
            evidence.append({"setup": name, "samples": row["samples"], "brier_gap": round(gap, 6)})
    return {"surcharge": round(min(0.015, penalty), 5), "evidence": evidence,
            "note": "Only weak, settled, sufficiently sampled setup-conditioned forecasts add a surcharge; positive results do not auto-increase risk."}
