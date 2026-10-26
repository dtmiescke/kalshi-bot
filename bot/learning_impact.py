"""Read-only counterfactual replay for learned signal-weight changes.

Compares saved post-update crypto decision snapshots using the weights just
before the latest learning update and the current learned weights. It never
places or simulates orders and does not rewrite historical decisions.
"""
import json
import time

from . import brain
from .signals.base import Reading


def _weights_at(store, cutoff_ns, domain):
    rows = store.q(
        """SELECT signal, weight FROM weight_history w
           WHERE ts <= ? AND ts = (
             SELECT MAX(ts) FROM weight_history WHERE signal = w.signal AND ts <= ?
           )""",
        (int(cutoff_ns), int(cutoff_ns)),
    )
    class Snapshot:
        def current_weights(self):
            return {r["signal"]: r["weight"] for r in rows}
    return brain.get_weights(Snapshot(), domain)


def _readings(signals):
    out = []
    if not isinstance(signals, dict):
        return out
    for name, value in signals.items():
        if not isinstance(value, dict) or value.get("prob") is None:
            continue
        try:
            out.append(Reading(name, float(value["prob"]),
                               float(value.get("confidence", 1.0)),
                               value.get("detail") or {}))
        except (TypeError, ValueError):
            continue
    return out


def report(store, limit=100):
    """Return a grounded old-vs-current weight replay from persisted snapshots."""
    changes = store.q("SELECT ts, signal, weight, reason FROM weight_history ORDER BY ts DESC LIMIT 500")
    if not changes:
        return {"ok": True, "status": "no learned weight history", "compared": 0}
    latest_ns = int(changes[0]["ts"])
    # One learning batch writes its per-signal weights within milliseconds.
    batch = [r for r in changes if latest_ns - int(r["ts"]) <= 5_000_000_000]
    batch_start_ns = min(int(r["ts"]) for r in batch)
    before = _weights_at(store, batch_start_ns - 1, "crypto")
    after = brain.get_weights(store, "crypto")
    start_s = batch_start_ns // 1_000_000_000
    rows = store.q(
        """SELECT id, ts, ticker, series, decision, reason, market_price,
                  model_prob, side_prob, edge, signals_json
           FROM decision_audit
           WHERE ts >= ? AND (series LIKE '%15M' OR series LIKE '%BTCD' OR series LIKE '%ETHD')
           ORDER BY ts DESC, id DESC LIMIT ?""",
        (start_s, max(1, min(int(limit), 500))),
    )
    compared = []
    for row in rows:
        try:
            signals = json.loads(row["signals_json"] or "{}")
        except (TypeError, ValueError):
            continue
        readings = _readings(signals)
        if not readings:
            continue
        old_prob, _ = brain.blend(readings, before)
        new_prob, _ = brain.blend(readings, after)
        if old_prob is None or new_prob is None:
            continue
        market = row["market_price"]
        old_side = "yes" if old_prob >= 0.5 else "no"
        new_side = "yes" if new_prob >= 0.5 else "no"
        compared.append({
            "ticker": row["ticker"], "ts": row["ts"],
            "recorded_decision": row["decision"], "recorded_reason": row["reason"],
            "saved_model_prob": row["model_prob"],
            "prob_with_pre_update_weights": round(old_prob, 6),
            "prob_with_current_weights": round(new_prob, 6),
            "probability_change_pp": round((new_prob - old_prob) * 100, 4),
            "side_before": old_side, "side_after": new_side,
            "side_changed": old_side != new_side,
            "market_yes_price": market,
            "edge_before_pp": round((old_prob - float(market)) * 100, 4) if market is not None else None,
            "edge_after_pp": round((new_prob - float(market)) * 100, 4) if market is not None else None,
        })
    flips = sum(1 for r in compared if r["side_changed"])
    return {
        "ok": True, "status": "compared" if compared else "no post-update crypto decision snapshots",
        "learning_batch_start_ns": batch_start_ns,
        "weights_changed": [{"signal": r["signal"], "weight": r["weight"], "reason": r["reason"]}
                            for r in sorted(batch, key=lambda x: x["signal"])],
        "snapshots_compared": len(compared), "side_flips": flips,
        "snapshots": compared,
        "note": "Read-only counterfactual using saved signal readings. It compares model probabilities and sides, not full risk-gate acceptance; no orders are placed.",
    }
