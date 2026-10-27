"""Empirical price-discovery diagnostics; not a standalone trading signal."""
from collections import defaultdict
from statistics import median


def estimate_price_discovery(reference_events, kalshi_events, horizon_seconds=30.0,
                             min_reference_move_bps=1.0, min_contract_move=0.01):
    """Estimate whether observed spot moves tend to precede Kalshi YES-mid moves.

    All comparisons use local receive timestamps to include real ingestion delay.
    Only events already observed are used; output is descriptive and needs a
    sufficiently large walk-forward sample before it can influence execution.
    """
    refs = sorted((e for e in (reference_events or [])
                   if isinstance(e, dict) and e.get("source") and e.get("price") and e.get("received_ts") is not None),
                  key=lambda e: float(e["received_ts"]))
    ks = sorted((e for e in (kalshi_events or [])
                 if isinstance(e, dict) and e.get("yes_mid") is not None and e.get("received_ts") is not None),
                key=lambda e: float(e["received_ts"]))
    grouped = defaultdict(list)
    previous = {}
    for event in refs:
        source = str(event["source"])
        price = float(event["price"])
        prior = previous.get(source)
        previous[source] = (float(event["received_ts"]), price)
        if prior is None or prior[1] <= 0 or price <= 0:
            continue
        move_bps = (price / prior[1] - 1.0) * 10000.0
        if abs(move_bps) < min_reference_move_bps:
            continue
        t0 = float(event["received_ts"])
        baseline = None
        for k in reversed(ks):
            if float(k["received_ts"]) <= t0:
                baseline = float(k["yes_mid"])
                break
        if baseline is None:
            continue
        follow = None
        for k in ks:
            dt = float(k["received_ts"]) - t0
            if dt <= 0:
                continue
            if dt > horizon_seconds:
                break
            delta = float(k["yes_mid"]) - baseline
            if abs(delta) >= min_contract_move:
                follow = {"lag_s": dt, "same_direction": (delta * move_bps) > 0}
                break
        if follow is not None:
            grouped[source].append(follow)
    out = {}
    for source, observations in grouped.items():
        lags = [x["lag_s"] for x in observations]
        out[source] = {"observations": len(observations),
                       "median_follow_lag_s": round(median(lags), 3),
                       "same_direction_rate": round(sum(x["same_direction"] for x in observations) / len(observations), 3)}
    return {"method": "receive-time spot-to-Kalshi YES-mid follow-through",
            "horizon_seconds": float(horizon_seconds), "sources": out,
            "warning": "Exploratory only; no source is declared faster or profitable without enough out-of-sample observations."}
