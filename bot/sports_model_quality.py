"""Leakage-resistant diagnostics for sports probability and market-price quality.

This module is deliberately side-effect free: it does not fetch data, change model
weights, qualify trades, or place orders. It provides common metrics for the existing
sports-learning pipeline and shadow evaluations.
"""
from __future__ import annotations

import math
from collections import defaultdict


def _valid_probability(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def brier_score(predictions):
    """Mean squared probability error; lower is better. Rows: (probability, outcome)."""
    errors = []
    for probability, outcome in predictions:
        p = _valid_probability(probability)
        y = _valid_probability(outcome)
        if p is None or y not in (0.0, 1.0):
            continue
        errors.append((p - y) ** 2)
    return sum(errors) / len(errors) if errors else None


def log_loss(predictions, epsilon=1e-15):
    """Binary log loss with bounded probabilities; lower is better."""
    if not 0 < epsilon < 0.5:
        raise ValueError("epsilon must be between 0 and 0.5")
    losses = []
    for probability, outcome in predictions:
        p = _valid_probability(probability)
        y = _valid_probability(outcome)
        if p is None or y not in (0.0, 1.0):
            continue
        p = min(1.0 - epsilon, max(epsilon, p))
        losses.append(-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)))
    return sum(losses) / len(losses) if losses else None


def calibration_bins(predictions, bins=10):
    """Return count, mean prediction, and observed rate for equal-width bins."""
    if not isinstance(bins, int) or bins < 2 or bins > 100:
        raise ValueError("bins must be an integer from 2 through 100")
    groups = defaultdict(list)
    for probability, outcome in predictions:
        p = _valid_probability(probability)
        y = _valid_probability(outcome)
        if p is None or y not in (0.0, 1.0):
            continue
        index = min(bins - 1, int(p * bins))
        groups[index].append((p, y))
    return [
        {
            "bin": index,
            "lower": index / bins,
            "upper": (index + 1) / bins,
            "count": len(rows),
            "mean_probability": sum(p for p, _ in rows) / len(rows),
            "observed_rate": sum(y for _, y in rows) / len(rows),
        }
        for index, rows in sorted(groups.items())
    ]


def calibration_error(predictions, bins=10):
    """Weighted absolute calibration gap across occupied bins."""
    rows = calibration_bins(predictions, bins=bins)
    count = sum(row["count"] for row in rows)
    if not count:
        return None
    return sum(row["count"] * abs(row["mean_probability"] - row["observed_rate"])
               for row in rows) / count


def closing_line_value(entry_probability, closing_probability):
    """Probability-space CLV: positive means the entry price was more favorable.

    Probabilities must be comparable, no-vig estimates for the same outcome/market.
    This is not ROI and is not a guarantee of profitability.
    """
    entry = _valid_probability(entry_probability)
    close = _valid_probability(closing_probability)
    if entry is None or close is None or entry <= 0 or entry >= 1 or close <= 0 or close >= 1:
        return None
    return close - entry


def summarize_predictions(predictions, bins=10, min_samples=100):
    """Compute comparable diagnostics without automatic model promotion."""
    valid = []
    for row in predictions:
        if not isinstance(row, dict):
            continue
        p = _valid_probability(row.get("probability"))
        y = _valid_probability(row.get("outcome"))
        if p is not None and y in (0.0, 1.0):
            valid.append((p, y))
    return {
        "sample_count": len(valid),
        "brier_score": brier_score(valid),
        "log_loss": log_loss(valid),
        "calibration_error": calibration_error(valid, bins=bins),
        "calibration_bins": calibration_bins(valid, bins=bins),
        "minimum_sample_count": max(1, int(min_samples)),
        "evaluation_ready": len(valid) >= max(1, int(min_samples)),
        "promotion_allowed": False,
        "note": "Diagnostics only; promotion requires time-ordered out-of-sample comparison and explicit review.",
    }


# Research catalog only. These sources are not enabled or fetched automatically.
# Existing ESPN + Odds API collectors remain canonical until an adapter is explicitly
# implemented, tested, budgeted, and deduplicated against existing records.
OPTIONAL_SPORTS_SOURCES = {
    "sportsdataverse": {
        "url": "https://github.com/sportsdataverse/sportsdataverse-py",
        "sports": ("NFL", "NCAA football", "NBA", "WNBA", "NCAA basketball", "MLB", "NHL", "soccer"),
        "use": "historical/statistical data access and sport-specific datasets",
        "mode": "opt-in adapter required",
    },
    "nflverse": {
        "url": "https://github.com/nflverse/nflfastR",
        "sports": ("NFL",),
        "use": "play-by-play, EPA, win probability, historical team/player features",
        "mode": "opt-in historical adapter required",
    },
    "nba_api": {
        "url": "https://github.com/swar/nba_api",
        "sports": ("NBA",),
        "use": "player/team statistics, game logs, shot charts, play-by-play",
        "mode": "opt-in adapter required; upstream access may be rate-limited",
    },
    "pybaseball": {
        "url": "https://github.com/jldbc/pybaseball",
        "sports": ("MLB",),
        "use": "Statcast, pitcher/batter splits, and historical baseball features",
        "mode": "opt-in adapter required",
    },
    "openligadb": {
        "url": "https://www.openligadb.de/",
        "sports": ("soccer",),
        "use": "fixtures and results",
        "mode": "optional public feed; not sportsbook odds",
    },
    "football_data": {
        "url": "https://www.football-data.co.uk/",
        "sports": ("soccer",),
        "use": "historical results and available historical bookmaker odds",
        "mode": "historical dataset; verify per-file terms and coverage",
    },
    "clubelo": {
        "url": "https://clubelo.com/",
        "sports": ("soccer",),
        "use": "independent historical team-strength baseline",
        "mode": "optional source; confirm permitted access and refresh policy",
    },
}
