"""Every research signal returns a Reading for the YES side of one market."""
import math
from dataclasses import dataclass, field


@dataclass
class Reading:
    name: str
    prob: float          # signal's estimate that YES wins (0-1)
    confidence: float    # 0-1, how much this reading should count right now
    detail: dict = field(default_factory=dict)


def clamp(p, lo=0.01, hi=0.99):
    return max(lo, min(hi, p))


def logit(p):
    p = clamp(p, 1e-4, 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-x))
