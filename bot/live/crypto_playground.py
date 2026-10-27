"""Offline-first crypto strategy playground for historical and newly captured OHLCV.

Uses chronological walk-forward splits, next-bar execution, configurable costs, and
keeps tuning data separate from out-of-sample evaluation. It never places orders.
CSV columns: timestamp (optional), open, high, low, close, volume.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean, median

from .chart_setups import analyze_chart_setups


def load_ohlcv_csv(path):
    bars = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        names = {str(n).strip().lower(): n for n in (reader.fieldnames or [])}
        missing = {"open", "high", "low", "close", "volume"} - set(names)
        if missing:
            raise ValueError("CSV missing required columns: " + ", ".join(sorted(missing)))
        for row in reader:
            try:
                item = {key: float(row[names[key]]) for key in ("open", "high", "low", "close", "volume")}
                if "timestamp" in names:
                    item["timestamp"] = row[names["timestamp"]]
                if all(math.isfinite(v) for v in item.values() if isinstance(v, (int, float))):
                    bars.append(item)
            except (TypeError, ValueError):
                continue
    if len(bars) < 80:
        raise ValueError("Need at least 80 valid OHLCV candles for a useful chronological evaluation.")
    return bars


def _signal(bars, end, threshold):
    result = analyze_chart_setups(bars[:end])
    if not result.get("ready"):
        return 0, result
    up, down = result.get("up_score", 0), result.get("down_score", 0)
    gap = up - down
    if gap >= threshold:
        return 1, result
    if gap <= -threshold:
        return -1, result
    return 0, result


def _run_segment(bars, start, end, threshold, fee_bps, slippage_bps, min_hold=1):
    trades = []
    # Signal uses bars ending at i; entry is the next candle open, never the signal close.
    for i in range(max(start, 50), min(end, len(bars) - min_hold)):
        side, evidence = _signal(bars, i + 1, threshold)
        if side == 0:
            continue
        entry = float(bars[i + 1]["open"])
        exit_index = min(i + 1 + min_hold, len(bars) - 1)
        exit_price = float(bars[exit_index]["close"])
        if entry <= 0 or exit_price <= 0:
            continue
        gross = side * (exit_price / entry - 1.0)
        costs = (fee_bps + slippage_bps) * 2.0 / 10000.0
        net = gross - costs
        trades.append({
            "signal_index": i, "entry_index": i + 1, "exit_index": exit_index,
            "side": "up" if side > 0 else "down", "entry": entry, "exit": exit_price,
            "gross_return": gross, "cost_return": costs, "net_return": net,
            "setups": [x["name"] for x in evidence.get("setups", [])],
            "bias": evidence.get("bias", "mixed"),
        })
    return trades


def _metrics(trades):
    returns = [float(t["net_return"]) for t in trades]
    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for ret in returns:
        equity *= max(0.0, 1.0 + ret)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    return {
        "trades": len(returns), "win_rate": len(wins) / len(returns) if returns else 0.0,
        "mean_net_return": mean(returns) if returns else 0.0,
        "median_net_return": median(returns) if returns else 0.0,
        "profit_factor": gross_wins / gross_losses if gross_losses else (None if not gross_wins else float("inf")),
        "max_drawdown": max_dd, "compounded_return": equity - 1.0,
        "positive_trades": len(wins), "negative_trades": len(losses),
    }


def walk_forward_playground(bars, fee_bps=5.0, slippage_bps=5.0,
                            train_fraction=0.6, threshold_grid=(0.25, 0.5, 0.75, 1.0),
                            min_trades=10):
    """Tune threshold on earlier data, then score the untouched later period."""
    if len(bars) < 100:
        raise ValueError("Need at least 100 candles for walk-forward research.")
    if not 0 < train_fraction < 0.8:
        raise ValueError("train_fraction must be between 0 and 0.8.")
    split = int(len(bars) * train_fraction)
    if split < 60 or len(bars) - split < 25:
        raise ValueError("Insufficient training or test candles.")
    candidates = []
    for threshold in threshold_grid:
        train_trades = _run_segment(bars, 0, split - 1, float(threshold), fee_bps, slippage_bps)
        metrics = _metrics(train_trades)
        score = metrics["mean_net_return"] if metrics["trades"] >= min_trades else -math.inf
        candidates.append({"threshold": float(threshold), "train_metrics": metrics, "selection_score": score})
    viable = [c for c in candidates if math.isfinite(c["selection_score"])]
    if not viable:
        chosen = min(candidates, key=lambda x: abs(x["threshold"] - 0.5))
        selection_note = "No threshold reached minimum training trade count; default-nearest candidate selected, results are exploratory."
    else:
        chosen = max(viable, key=lambda x: x["selection_score"])
        selection_note = "Threshold selected on training period only."
    test_trades = _run_segment(bars, split, len(bars) - 1, chosen["threshold"], fee_bps, slippage_bps)
    return {
        "mode": "offline_historical_walk_forward", "order_execution": False,
        "candles": len(bars), "split_index": split,
        "training_candles": split, "test_candles": len(bars) - split,
        "chosen_threshold": chosen["threshold"], "selection_note": selection_note,
        "cost_assumptions": {"fee_bps_per_side": fee_bps, "slippage_bps_per_side": slippage_bps,
                             "note": "Illustrative configurable costs; replace with venue-specific realized fees and fill/slippage logs."},
        "training_metrics": chosen["train_metrics"],
        "out_of_sample_metrics": _metrics(test_trades),
        "candidate_thresholds": candidates,
        "out_of_sample_trades": test_trades,
        "limitations": [
            "Spot-direction proxy, not a full Kalshi contract fill simulation.",
            "No queue position, partial fills, latency, contract fees, funding, or market impact simulation.",
            "Historical OHLCV cannot reveal exact exchange message-arrival order.",
            "A profitable backtest is not evidence of future profitability; test across regimes and assets."
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline crypto chart/strategy playground; never places orders.")
    parser.add_argument("csv", help="Historical or newly captured OHLCV CSV")
    parser.add_argument("--fee-bps", type=float, default=5.0, help="Estimated fee basis points per side")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Estimated slippage basis points per side")
    parser.add_argument("--train-fraction", type=float, default=0.6)
    parser.add_argument("--out", default="crypto_playground_report.json")
    args = parser.parse_args(argv)
    bars = load_ohlcv_csv(args.csv)
    report = walk_forward_playground(bars, args.fee_bps, args.slippage_bps, args.train_fraction)
    Path(args.out).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("mode", "candles", "chosen_threshold", "training_metrics", "out_of_sample_metrics", "selection_note")}, indent=2, allow_nan=False))
    print("Detailed report written to", args.out)


if __name__ == "__main__":
    main()
