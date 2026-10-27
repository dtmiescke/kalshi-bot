"""Audit 90% prediction and paper-bet targets from the existing SQLite ledger.

Read-only report. It never places orders, changes thresholds, or mutates the DB.
Prediction accuracy is directional accuracy from canonical scored forecasts;
90%-confidence accuracy uses max(P(YES), P(NO)) >= 0.90. Paper-bet win rate
uses only settled paper rows and the chosen side. This is descriptive evidence,
not a promise that future accuracy will match the sample.
"""
import argparse
import json
import math
import sqlite3
from pathlib import Path


TARGET = 0.90
MIN_SAMPLE = 100


def _wilson_lower(wins, n, z=1.96):
    """Two-sided 95% Wilson lower bound for a binomial success rate."""
    if n <= 0:
        return None
    p = wins / n
    den = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (center - margin) / den


def _rate(rows, success):
    n = len(rows)
    wins = sum(bool(success(r)) for r in rows)
    return {
        "n": n,
        "wins": wins,
        "rate": wins / n if n else None,
        "wilson_95_lower": _wilson_lower(wins, n),
        "target_met_observed": bool(n and wins / n >= TARGET),
        "target_supported_95pct": bool(n >= MIN_SAMPLE and _wilson_lower(wins, n) >= TARGET),
    }


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def evaluate_connection(db):
    tables = _tables(db)
    predictions = []
    bets = []
    if "decision_audit" in tables:
        cols = {r[1] for r in db.execute("PRAGMA table_info(decision_audit)")}
        required = {"model_prob", "outcome", "brier"}
        if required <= cols:
            predictions = [dict(r) for r in db.execute(
                """SELECT ticker, model_prob, outcome, brier, settled_ts
                   FROM decision_audit
                   WHERE brier IS NOT NULL AND model_prob IS NOT NULL
                     AND outcome IN (0,1)
                   ORDER BY settled_ts, ticker"""
            )]
    if "bets" in tables:
        cols = {r[1] for r in db.execute("PRAGMA table_info(bets)")}
        required = {"mode", "side", "result", "status", "pnl"}
        if required <= cols:
            bets = [dict(r) for r in db.execute(
                """SELECT id, ticker, side, result, pnl, fee, stake, price, contracts
                   FROM bets
                   WHERE lower(mode)='paper' AND lower(status)='graded'
                     AND lower(side) IN ('yes','no')
                     AND lower(result) IN ('yes','no')
                   ORDER BY graded_ts, id"""
            )]
    directional = [r for r in predictions if 0 <= float(r["model_prob"]) <= 1]
    high_conf = [r for r in directional if max(float(r["model_prob"]), 1 - float(r["model_prob"])) >= TARGET]
    bet_rows = bets
    pred_stats = _rate(directional, lambda r: (float(r["model_prob"]) >= 0.5) == bool(r["outcome"]))
    high_stats = _rate(high_conf, lambda r: (float(r["model_prob"]) >= 0.5) == bool(r["outcome"]))
    bet_stats = _rate(bet_rows, lambda r: r["side"].lower() == r["result"].lower())
    # Realized P&L is taken from the ledger as stored; do not infer wins from P&L.
    net_pnl = sum(float(r["pnl"] or 0) for r in bet_rows)
    total_stake = sum(float(r["stake"] or 0) for r in bet_rows)
    return {
        "target": TARGET,
        "minimum_sample_for_supported_claim": MIN_SAMPLE,
        "prediction_accuracy": pred_stats,
        "high_confidence_prediction_accuracy": high_stats,
        "paper_bet_win_rate": bet_stats,
        "paper_net_pnl": round(net_pnl, 4),
        "paper_total_stake": round(total_stake, 4),
        "paper_roi": net_pnl / total_stake if total_stake > 0 else None,
        "verdict": {
            "prediction_target_supported": pred_stats["target_supported_95pct"],
            "high_confidence_target_supported": high_stats["target_supported_95pct"],
            "paper_bet_target_supported": bet_stats["target_supported_95pct"],
        },
        "notes": [
            "A target is marked statistically supported only with at least 100 settled samples and a 95% Wilson lower bound >= 90%.",
            "Prediction accuracy is directional accuracy, not probability calibration. Brier score and calibration must be reviewed separately.",
            "Paper win rate is not profitability; fees, stake-weighted net P&L, fills and drawdown also matter.",
            "Only unique canonical forecasts with non-null Brier scores count. Repeated unscored scans are excluded.",
            "A missing or empty ledger is reported as insufficient evidence, never as a pass.",
        ],
    }


def evaluate(db_path):
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite ledger not found: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        return evaluate_connection(db)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/bot.sqlite", help="Existing bot SQLite ledger")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    result = evaluate(args.db)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print("=== 90% PREDICTION / PAPER-BET VALIDATION ===")
    for key, title in (
        ("prediction_accuracy", "All directional predictions"),
        ("high_confidence_prediction_accuracy", "Predictions marked >=90% confidence"),
        ("paper_bet_win_rate", "Settled paper bets"),
    ):
        stat = result[key]
        if not stat["n"]:
            print(f"{title}: INSUFFICIENT DATA (0 settled samples)")
            continue
        lower = stat["wilson_95_lower"]
        print(f"{title}: {stat['wins']}/{stat['n']} = {stat['rate']:.1%}; "
              f"95% lower bound={lower:.1%}; "
              f"observed target={'PASS' if stat['target_met_observed'] else 'FAIL'}; "
              f"evidence={'SUPPORTED' if stat['target_supported_95pct'] else 'NOT YET SUPPORTED'}")
    print(f"Paper net P&L: ${result['paper_net_pnl']:.2f}; "
          f"ROI on stake: {result['paper_roi']:.2%}" if result["paper_roi"] is not None
          else f"Paper net P&L: ${result['paper_net_pnl']:.2f}; ROI unavailable (zero stake)")
    print("This report is read-only. A failed/insufficient target does not enable more aggressive betting.")


if __name__ == "__main__":
    main()
