"""Conservative edge-quality diagnostics for short-window crypto contracts.

This layer does not invent a win probability. It adds a small uncertainty margin
to the existing exact-contract model and fee-aware risk planner when independent
signals disagree or live reference feeds are weak.
"""
import math
from statistics import median


def edge_quality(readings, source_summary, max_divergence_bps=25.0):
    """Return a conservative minimum-edge surcharge and auditable diagnostics.

    Readings are model/signal objects with name and prob attributes.
    Market-derived anchors are excluded from disagreement to avoid counting the
    same Kalshi price twice. Correlated model variants are not treated as
    independent votes; disagreement is used only as a modest risk margin.
    """
    model_names = {
        "crypto_model", "crypto_model_iv", "ml_model", "crypto_trend_strategy",
        "crypto_mean_reversion_strategy", "crypto_breakout_strategy",
        "crypto_multi_timeframe_chart", "spot_flow", "crypto_orderbook", "crypto_playbook",
    }
    values = []
    for reading in readings or ():
        if getattr(reading, "name", None) not in model_names:
            continue
        try:
            p = float(reading.prob)
        except (TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(p) and 0.0 <= p <= 1.0:
            values.append(p)

    center = median(values) if values else None
    mad = median([abs(p - center) for p in values]) if center is not None else None
    signal_penalty = min(0.01, 0.25 * mad) if mad is not None else 0.005

    source_summary = source_summary if isinstance(source_summary, dict) else {}
    count = int(source_summary.get("source_count") or 0)
    try:
        spread = float(source_summary.get("spread_bps"))
        if not math.isfinite(spread) or spread < 0:
            spread = None
    except (TypeError, ValueError):
        spread = None
    threshold = max(float(max_divergence_bps), 0.01)
    if spread is None:
        feed_penalty = 0.003
    else:
        feed_penalty = min(0.005, spread / threshold * 0.005)
    source_penalty = 0.003 if count < 2 else 0.0
    surcharge = min(0.015, signal_penalty + feed_penalty + source_penalty)
    return {
        "min_edge_surcharge": round(surcharge, 5),
        "signal_count": len(values),
        "signal_median_probability": round(center, 5) if center is not None else None,
        "signal_mad": round(mad, 5) if mad is not None else None,
        "source_count": count,
        "source_spread_bps": round(spread, 3) if spread is not None else None,
        "signal_penalty": round(signal_penalty, 5),
        "feed_penalty": round(feed_penalty, 5),
        "source_penalty": round(source_penalty, 5),
    }


def enrich_plan(plan, diagnostics, probability):
    """Attach realized-cost EV diagnostics to a fee-aware risk plan."""
    if not isinstance(plan, dict):
        return plan
    contracts = max(0, int(plan.get("contracts") or 0))
    edge = float(plan.get("edge") or 0.0)
    cost = float(plan.get("stake") or 0.0) + float(plan.get("fee") or 0.0)
    plan["edge_quality"] = dict(diagnostics)
    plan["edge_before_uncertainty_margin"] = round(edge, 5)
    plan["expected_value_after_fees"] = round(edge * contracts, 4)
    plan["total_cost_including_fees"] = round(cost, 2)
    plan["expected_return_on_cost"] = round(edge * contracts / cost, 5) if cost > 0 else None
    plan["break_even_probability"] = round(
        (float(plan.get("price") or 0.0) * contracts + float(plan.get("fee") or 0.0))
        / contracts, 5
    ) if contracts else None
    plan["model_probability_for_side"] = round(
        float(probability) if plan.get("side") == "yes" else 1.0 - float(probability), 5
    ) if probability is not None else None
    return plan
