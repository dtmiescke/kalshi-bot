"""Paper-only decision profiles layered over the bot's existing source ensemble.

Modes adjust decision strictness, not hard safety controls. The underlying model
still blends the configured signals (including AI news when available), and the
risk engine still enforces fee-inclusive edge, exposure, cash, and loss limits.
"""
from dataclasses import dataclass
import math

LEVELS = ("off", "conservative", "balanced", "aggressive", "adaptive")


@dataclass(frozen=True)
class AggressionPolicy:
    level: str = "aggressive"
    min_edge: float = 0.03
    min_confidence: float = 0.55
    kelly_fraction: float = 0.25

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError("unknown aggression level")
        if not all(math.isfinite(x) for x in (
                self.min_edge, self.min_confidence, self.kelly_fraction)):
            raise ValueError("aggression inputs must be finite")
        if not (0 < self.min_edge < 1 and 0 < self.min_confidence <= 1
                and 0 <= self.kelly_fraction <= 1):
            raise ValueError("invalid baseline risk parameters")


def choose(policy, settled_paper_results=()):
    """Return a bounded profile; never authorize a trade or weaken hard guards.

    Adaptive promotes to aggressive only after >=50 settled, held-out paper
    results with positive fee-inclusive PnL and <=10% peak-to-trough drawdown.
    Without verified held-out history it deliberately fails closed to
    conservative. The runtime currently supplies no held-out history, so
    adaptive remains conservative until a trustworthy history source is wired.
    """
    if policy.level == "off":
        return None

    level = policy.level
    if level == "adaptive":
        rows = list(settled_paper_results)
        valid = len(rows) >= 50 and all(
            isinstance(r, dict)
            and r.get("mode") == "paper"
            and r.get("held_out") is True
            and r.get("settled") is True
            and isinstance(r.get("pnl"), (int, float))
            and not isinstance(r.get("pnl"), bool)
            and math.isfinite(r["pnl"])
            for r in rows
        )
        if valid:
            equity = peak = 0.0
            worst = 0.0
            for r in rows:
                equity += r["pnl"]
                peak = max(peak, equity)
                worst = max(worst, peak - equity)
            valid = equity > 0 and worst <= max(1.0, abs(equity)) * 0.10
        level = "aggressive" if valid else "conservative"

    # Thresholds are applied to the ensemble's source-informed probability and
    # confidence. Aggressive never reduces the baseline fee-inclusive edge floor.
    factors = {
        "conservative": (1.25, 1.10, 0.60),
        "balanced": (1.00, 1.00, 1.00),
        "aggressive": (1.00, 0.96, 1.00),
    }
    edge_factor, confidence_factor, size_factor = factors[level]
    return {
        "effective_level": level,
        "min_edge": max(policy.min_edge, min(0.99, policy.min_edge * edge_factor)),
        "min_confidence": min(1.0, policy.min_confidence * confidence_factor),
        "kelly_fraction": min(policy.kelly_fraction,
                              policy.kelly_fraction * size_factor),
    }
