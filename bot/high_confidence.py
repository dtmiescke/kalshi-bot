"""Evidence-based high-confidence evaluation for the existing paper-only bot.

A 90% target is an acceptance criterion, not a forecast guarantee. This report
scores one canonical settled prediction per market and actual paper-ledger bets
separately. Repeated scans do not count as new prediction samples.
"""
import math

TARGET_ACCURACY = 0.90
MIN_SAMPLE_FOR_TARGET = 100
Z_95 = 1.959963984540054


def wilson_interval(wins, n, z=Z_95):
    """Two-sided 95% Wilson interval for a binomial win rate."""
    if n <= 0:
        return None, None
    p = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _summary(items, target=TARGET_ACCURACY, min_sample=MIN_SAMPLE_FOR_TARGET):
    n = len(items)
    wins = sum(int(item["won"]) for item in items)
    rate = wins / n if n else None
    low, high = wilson_interval(wins, n)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "accuracy": rate,
        "ci95_low": low,
        "ci95_high": high,
        # Require both adequate sample size and a 95% lower confidence bound
        # at/above target; a lucky small sample cannot pass.
        "target_met": bool(n >= min_sample and low is not None and low >= target),
        "target": target,
        "minimum_sample": min_sample,
    }


def evaluate_predictions(rows, target=TARGET_ACCURACY, min_sample=MIN_SAMPLE_FOR_TARGET):
    """Evaluate canonical YES-probability forecasts against binary outcomes."""
    valid = []
    for row in rows:
        try:
            p = float(row["model_prob"])
            y = int(row["outcome"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(p) or not 0 <= p <= 1 or y not in (0, 1):
            continue
        valid.append({"won": int((p >= 0.5) == bool(y)), "confidence": max(p, 1 - p)})
    high = [r for r in valid if r["confidence"] >= target]
    return {
        "all": _summary(valid, target, min_sample),
        "high_confidence": _summary(high, target, min_sample),
    }


def evaluate_paper_bets(rows, target=TARGET_ACCURACY, min_sample=MIN_SAMPLE_FOR_TARGET):
    """Evaluate graded paper bets by chosen-side model probability and outcome."""
    valid = []
    for row in rows:
        try:
            p = float(row["model_prob"])
            result = str(row["result"]).lower()
            side = str(row["side"]).lower()
        except (KeyError, TypeError, ValueError):
            continue
        if (not math.isfinite(p) or not 0 <= p <= 1 or
                result not in ("yes", "no") or side not in ("yes", "no")):
            continue
        side_prob = p if side == "yes" else 1 - p
        valid.append({
            "won": int(side == result),
            "confidence": side_prob,
            "pnl": float(row.get("pnl") or 0.0),
        })
    high = [r for r in valid if r["confidence"] >= target]
    all_summary = _summary(valid, target, min_sample)
    high_summary = _summary(high, target, min_sample)
    all_summary["net_pnl"] = sum(r["pnl"] for r in valid)
    high_summary["net_pnl"] = sum(r["pnl"] for r in high)
    return {"all": all_summary, "high_confidence": high_summary}


def report(store, target=TARGET_ACCURACY, min_sample=MIN_SAMPLE_FOR_TARGET):
    """Read settled canonical prediction audits and graded paper bets from Store."""
    predictions = store.q("""SELECT model_prob, outcome FROM decision_audit
        WHERE brier IS NOT NULL AND model_prob IS NOT NULL AND outcome IN (0,1)""")
    paper_bets = store.q("""SELECT model_prob, result, side, pnl FROM bets
        WHERE mode='paper' AND status='graded' AND result IN ('yes','no')
        AND model_prob IS NOT NULL AND side IN ('yes','no')""")
    return {
        "target_accuracy": target,
        "minimum_sample": min_sample,
        "predictions": evaluate_predictions(predictions, target, min_sample),
        "paper_bets": evaluate_paper_bets(paper_bets, target, min_sample),
    }


def format_report(result):
    """Render a concise operator-readable status; never imply unproven success."""
    lines = [f"90% HIGH-CONFIDENCE VALIDATION | target={result['target_accuracy']:.0%} | "
             f"minimum sample={result['minimum_sample']}"]
    for section in ("predictions", "paper_bets"):
        lines.append(f"\n{section.replace('_', ' ').upper()}")
        for cohort in ("all", "high_confidence"):
            s = result[section][cohort]
            if not s["n"]:
                lines.append(f"  {cohort}: no graded samples yet; NOT PROVEN")
                continue
            low = s["ci95_low"]
            high = s["ci95_high"]
            label = "TARGET VALIDATED" if s["target_met"] else "NOT YET VALIDATED"
            lines.append(
                f"  {cohort}: {s['wins']}/{s['n']} correct ({s['accuracy']:.1%}); "
                f"95% CI {low:.1%}-{high:.1%}; {label}"
            )
            if "net_pnl" in s:
                lines.append(f"    net paper P&L: ${s['net_pnl']:.2f}")
    return "\n".join(lines)
