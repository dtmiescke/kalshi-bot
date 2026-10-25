"""Conservative opportunity-aware tuning for the existing paper-only risk path.

No new storage, feeds, models, or execution route. This module can only tighten
an existing policy; it cannot loosen baseline thresholds or raise bet sizes.
"""
import math


def contextual_policy(policy, *, model_prob, confidence, book, history=()):
    """Tighten a paper adaptive policy for uncertain forecasts / thin books.

    Missing, malformed, or non-finite data is treated as uncertain. The
    settled-history promotion rule remains in adaptive_aggression.choose().
    """
    if policy is None:
        return None
    result = dict(policy)
    try:
        p = float(model_prob)
        c = float(confidence)
        if not (math.isfinite(p) and 0 <= p <= 1 and
                math.isfinite(c) and 0 <= c <= 1):
            raise ValueError("invalid forecast")
        levels = (book or {}).get("yes", []) + (book or {}).get("no", [])
        valid_depth = sum(
            int(q) for price, q in levels
            if isinstance(price, (int, float)) and math.isfinite(price)
            and 0 < price < 1 and isinstance(q, (int, float))
            and math.isfinite(q) and q > 0
        )
    except (TypeError, ValueError, AttributeError):
        p, c, valid_depth = 0.5, 0.0, 0

    # Avoid overconfidence and illiquid opportunities, with bounded changes.
    uncertainty = 1.0 - min(1.0, abs(p - 0.5) * 2)
    confidence_penalty = max(0.0, 0.65 - c) / 0.65
    depth_penalty = 1.0 if valid_depth < 10 else (0.5 if valid_depth < 50 else 0.0)
    caution = min(1.0, max(uncertainty * 0.4, confidence_penalty, depth_penalty))
    result["min_edge"] = min(0.99, max(result["min_edge"], result["min_edge"] * (1 + 0.5 * caution)))
    result["min_confidence"] = min(1.0, max(result["min_confidence"], result["min_confidence"] + 0.1 * caution))
    result["kelly_fraction"] = max(0.0, min(result["kelly_fraction"], result["kelly_fraction"] * (1 - 0.5 * caution)))
    result["context_caution"] = round(caution, 4)
    return result
