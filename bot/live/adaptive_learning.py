"""Online, regime-aware strategy evaluation with conservative promotion gates.

This module learns signal reliability from resolved outcomes. It does not place
orders, modify risk limits, or claim that a score is a calibrated probability.
Persist state externally and version it; only train on resolved observations.
"""
from __future__ import annotations
from collections import defaultdict, deque
from math import isfinite
from statistics import mean, median


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


def historical_setup_penalty(store, setup_names, regime="mixed", min_samples=20, report=None, coin=None):
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
    # Add a second, coin-specific check from actual graded paper-trade postmortems.
    # This only tightens the edge hurdle when a setup has enough observations AND
    # both net P&L and closing-line movement are weak; it never relaxes a gate.
    if coin and setup_names:
        try:
            import json
            reviews = store.q(
                "SELECT review_json FROM trade_reviews WHERE coin=? ORDER BY ts DESC LIMIT 5000",
                (str(coin).upper(),))
            buckets = defaultdict(list)
            for saved in reviews:
                try:
                    review = json.loads(saved.get("review_json") or "{}")
                    obs = review.get("observed") or {}
                    if str(obs.get("result") or "").lower() not in ("yes", "no"):
                        continue
                    payload = obs.get("signal_snapshot") or {}
                    raw = payload.get("chart_setups") or payload.get("setups") or []
                    names = set()
                    if isinstance(raw, str):
                        names.add(raw)
                    elif isinstance(raw, list):
                        for item in raw:
                            if isinstance(item, str):
                                names.add(item)
                            elif isinstance(item, dict) and item.get("name"):
                                names.add(str(item["name"]))
                    for name in names:
                        buckets[name].append({
                            "pnl": float(obs.get("pnl") or 0),
                            "clv": float(obs["clv"]) if obs.get("clv") is not None else None,
                        })
                except (TypeError, ValueError, AttributeError):
                    continue
            for name in set(str(x) for x in setup_names):
                samples = buckets.get(name, [])
                clv = [x["clv"] for x in samples if x["clv"] is not None]
                net = sum(x["pnl"] for x in samples)
                if len(samples) >= int(min_samples) and clv and net < 0 and sum(clv) / len(clv) < 0:
                    penalty += 0.0025
                    evidence.append({"setup": name, "coin": str(coin).upper(),
                        "samples": len(samples), "net_pnl": round(net, 2),
                        "avg_clv": round(sum(clv) / len(clv), 5),
                        "source": "graded_paper_trade_reviews"})
        except Exception:
            # Review-ledger failures must not break the existing analysis path.
            pass
    return {"surcharge": round(min(0.015, penalty), 5), "evidence": evidence,
            "note": "Only weak, settled, sufficiently sampled setup evidence can increase the required edge (max 1.5pp); positive results never lower risk gates."}


def graded_price_discovery(store, min_samples=20, limit=5000):
    """Summarize receive-time price-discovery evidence from settled canonical forecasts.

    Counts distinct graded market snapshots, not the nested rolling event count.
    This avoids pretending overlapping rolling windows are independent events.
    The report is descriptive only and must not directly authorize a trade.
    """
    import json
    rows = store.q(
        "SELECT signals_json, settled_ts FROM decision_audit "
        "WHERE outcome IS NOT NULL AND brier IS NOT NULL "
        "ORDER BY settled_ts DESC LIMIT ?", (int(limit),)
    )
    buckets = defaultdict(list)
    for row in rows:
        try:
            payload = row.get("signals_json") or "{}"
            data = json.loads(payload) if isinstance(payload, str) else payload
            discovery = data.get("price_discovery") or {}
            sources = discovery.get("sources") or {}
            for source, item in sources.items():
                if not isinstance(item, dict):
                    continue
                lag = float(item.get("median_follow_lag_s"))
                rate = float(item.get("same_direction_rate"))
                if not (isfinite(lag) and lag >= 0 and isfinite(rate) and 0 <= rate <= 1):
                    continue
                buckets[str(source)].append((lag, rate))
        except (TypeError, ValueError, AttributeError):
            continue
    report = {}
    for source, samples in buckets.items():
        lags = [x[0] for x in samples]
        rates = [x[1] for x in samples]
        n = len(samples)
        report[source] = {
            "graded_snapshots": n,
            "median_reported_follow_lag_s": round(median(lags), 3),
            "mean_same_direction_rate": round(mean(rates), 4),
            "eligible_for_review": n >= int(min_samples),
            "warning": "Rolling receive-time diagnostics are observational; latency, sampling and market selection can bias them."
        }
    return report
