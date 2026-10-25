"""Opt-in adaptive aggression policy. Does not place orders or alter hard risk caps.

Aggressive is the requested policy default, but existing runtime remains unchanged until wired.
Only settled, chronological, out-of-sample paper outcomes may drive adaptation.
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
    """Return optional decision thresholds; never modify the input policy.

    Adaptive only relaxes paper selection after >=50 chronological, independent
    held-out settlements with positive fee-inclusive PnL and <=10% drawdown.
    Results: sequence of dicts with pnl, held_out, mode and settled.
    In all other cases adaptive stays conservative. No automatic live escalation.
    """
    if policy.level == "off":
        return None
    level = policy.level
    if level == "adaptive":
        rows = list(settled_paper_results)
        valid = len(rows) >= 50 and all(
            r.get("mode") == "paper" and r.get("held_out") is True
            and r.get("settled") is True
            and isinstance(r.get("pnl"), (int, float))
            and not isinstance(r.get("pnl"), bool)
            and math.isfinite(r["pnl"]) for r in rows)
        if valid:
            equity = peak = 0.0
            worst = 0.0
            for r in rows:
                equity += r["pnl"]
                peak = max(peak, equity)
                worst = max(worst, peak - equity)
            valid = equity > 0 and worst <= max(1.0, abs(equity)) * 0.10
        level = "aggressive" if valid else "conservative"
    factors = {
        "conservative": (1.25, 1.05, 0.60),
        "balanced": (1.0, 1.0, 1.0),
        "aggressive": (0.85, 0.96, 1.0),
    }
    edge, confidence, size = factors[level]
    return {
        "effective_level": level,
        "min_edge": min(0.99, policy.min_edge * edge),
        "min_confidence": min(1.0, policy.min_confidence * confidence),
        "kelly_fraction": min(policy.kelly_fraction, policy.kelly_fraction * size),
    }
