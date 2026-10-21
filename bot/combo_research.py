"""Read-only, conservative Kalshi combo research and fee-aware opportunity ranking.

Never submits RFQs, accepts quotes, creates combo markets, or places orders.
A combo is ranked only when every leg has fresh model evidence, each leg belongs
to a distinct underlying event, and the actual combo market exposes a usable ask.
"""
import json
import time
from datetime import datetime, timezone

from . import brain, risk
from .signals.base import Reading

# No sport is blanket-excluded from combo research. Each leg must still pass
# freshness, evidence, distinct-event, price, fee, and uncertainty checks.
EXCLUDED_TERMS = ()


def _price(value):
    """Parse Kalshi dollar prices or legacy cents into a 0..1 price."""
    if value is None or value == "":
        return None
    try:
        p = float(str(value).replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    if p > 1:
        p /= 100.0
    return p if 0 < p < 1 else None


def _market_ask(market, side):
    """Use an actual displayed ask; if absent, mirror the opposite-side bid."""
    ask = _price(market.get(f"{side}_ask_dollars") or market.get(f"{side}_ask"))
    if ask is not None:
        return ask
    opposite = "no" if side == "yes" else "yes"
    bid = _price(market.get(f"{opposite}_bid_dollars") or market.get(f"{opposite}_bid"))
    return round(1.0 - bid, 4) if bid is not None else None


def score_combo_probabilities(leg_probabilities, yes_ask, no_ask,
                              fee_rate=0.07, min_edge=0.06):
    """Score an ALL-legs-win combo using conservative joint-probability bounds.

    Independence is only considered when the caller has already verified that
    the legs belong to distinct events. The product is then discounted by a
    leg-count-dependent uncertainty haircut. NO uses the opposite bound so
    uncertainty cannot artificially improve either side's edge.
    """
    probs = [float(p) for p in leg_probabilities]
    if len(probs) < 2 or any(not 0 < p < 1 for p in probs):
        return {"eligible": False, "reason": "invalid leg probabilities"}
    product = 1.0
    for p in probs:
        product *= p
    haircut = min(0.10, 0.03 + 0.015 * (len(probs) - 2))
    p_yes_conservative = max(0.0, product - haircut)
    p_yes_upper = min(1.0, product + haircut)
    scored = []
    for side, ask, win_prob in (
        ("yes", yes_ask, p_yes_conservative),
        ("no", no_ask, 1.0 - p_yes_upper),
    ):
        price = _price(ask)
        if price is None:
            continue
        fee = risk.taker_fee(1, price, fee_rate)
        edge = win_prob - price - fee
        cost = price + fee
        scored.append({
            "side": side, "ask": round(price, 4), "fee_per_contract": round(fee, 4),
            "conservative_win_probability": round(win_prob, 4),
            "expected_profit_per_contract": round(edge, 4),
            "expected_roi_on_cost": round(edge / cost, 4) if cost else None,
            "passes_min_edge": edge >= float(min_edge),
        })
    best = max(scored, key=lambda x: x["expected_profit_per_contract"], default=None)
    return {
        "eligible": bool(best and best["passes_min_edge"]),
        "reason": None if best and best["passes_min_edge"] else "no side clears fee-adjusted minimum edge",
        "independence_product": round(product, 4),
        "uncertainty_haircut": round(haircut, 4),
        "candidates": sorted(scored, key=lambda x: x["expected_profit_per_contract"], reverse=True),
        "best": best,
    }


def conservative_yes_probability(scored):
    """Return the uncertainty-adjusted YES probability used for combo grading."""
    try:
        product = float(scored["independence_product"])
        haircut = float(scored["uncertainty_haircut"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0.0 <= product <= 1.0 or not 0.0 <= haircut <= 1.0:
        return None
    return round(max(0.0, min(1.0, product - haircut)), 6)


def conservative_leg_probability(probability, confidence, signal_count=1):
    """Shrink a raw leg forecast toward 50% unless confidence and signal diversity support it.

    This is a deliberately conservative heuristic, not a learned calibration model.
    Historical calibration is applied separately before this shrinkage.
    """
    try:
        p = float(probability)
        conf = max(0.0, min(1.0, float(confidence)))
        n = max(0, int(signal_count))
    except (TypeError, ValueError):
        return 0.5
    if not 0.0 < p < 1.0:
        return 0.5
    diversity = min(1.0, 0.70 + 0.10 * max(0, n - 1))
    reliability = conf * diversity
    return round(0.5 + (p - 0.5) * reliability, 6)


def combo_ranking_score(leg_count, expected_profit, minimum_leg_confidence, min_edge):
    """Prefer 3-5 legs unless a longer combo has unusually strong evidence/value."""
    ev = float(expected_profit or 0.0)
    conf = float(minimum_leg_confidence or 0.0)
    exceptional = (
        int(leg_count) > 5 and conf >= 0.85
        and ev >= max(0.12, 2 * float(min_edge))
    )
    multiplier = 1.0 if int(leg_count) <= 5 or exceptional else {
        6: 0.85, 7: 0.75, 8: 0.65
    }.get(int(leg_count), 0.0)
    return {"ranking_score": round(ev * multiplier, 4),
            "length_preference_multiplier": multiplier,
            "exceptional_long_combo": exceptional}


def _leg_forecast(store, ticker, now, max_age_s=7200):
    """Blend the freshest per-signal readings for one underlying market."""
    rows = store.q(
        "SELECT ts, signal, prob, confidence, detail FROM signal_readings "
        "WHERE ticker=? AND ts>=? ORDER BY ts DESC",
        (ticker, now - max_age_s),
    )
    latest = {}
    for row in rows:
        if row["signal"] in latest:
            continue
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        latest[row["signal"]] = Reading(
            row["signal"], float(row["prob"]), float(row["confidence"]), detail
        )
    readings = list(latest.values())
    # A market quote alone is not enough to claim the bot has an informational edge.
    if not readings or not any(r.name != "kalshi_market" for r in readings):
        return None
    prob, confidence = brain.blend(readings, brain.get_weights(store, "sports"))
    if prob is None:
        return None
    return {"prob_yes": prob, "confidence": confidence, "signals": sorted(r.name for r in readings),
            "external_signal_count": len({r.name for r in readings if r.name != "kalshi_market"}),
            "latest_ts": max((int(row["ts"]) for row in rows), default=0)}


def _excluded(event, market, legs):
    parts = [
        event.get("title", ""), event.get("sub_title", ""), event.get("category", ""),
        market.get("title", ""), market.get("subtitle", ""), market.get("yes_sub_title", ""),
        market.get("no_sub_title", ""), market.get("ticker", ""),
    ]
    for leg in legs:
        parts.extend([leg.get("market_ticker", ""), leg.get("event_ticker", "")])
    text = " ".join(str(x).lower() for x in parts)
    return any(term in text for term in EXCLUDED_TERMS)


def combo_audit_cutoff_ts(close_time, lead_seconds=300):
    """Return the last timestamp at which a combo forecast may be recorded.

    A fixed lead-time keeps the evaluation out-of-sample: stale exchange
    listings cannot refresh a forecast after close, and forecasts near the
    outcome are not compared with much earlier candidates. Missing/invalid
    close times are not safe for audit grading.
    """
    if not close_time:
        return None
    try:
        parsed = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        close_ts = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return int(close_ts - max(0, int(lead_seconds)))


def scan(client, store, settings=None, max_events=2000, max_age_s=7200):
    now = int(time.time())
    fee_rate = float(getattr(settings, "taker_fee_rate", 0.07))
    min_edge = float(getattr(settings, "combo_min_edge", 0.06))
    min_legs = max(3, int(getattr(settings, "combo_min_legs", 3)))
    max_legs = max(min_legs, min(8, int(getattr(settings, "combo_max_legs", 8))))
    # Search the widest bounded batch returned by the API before ranking; this is
    # still constrained by upstream pagination/rate limits and available data.
    try:
        events = client.multivariate_events(status="open")
    except Exception as exc:
        result = {"enabled": True, "ok": False, "ts": now, "count": 0,
                  "ranked_count": 0, "execution_enabled": False, "error": str(exc)[:240]}
        store.set_kv("combo_research_health", result)
        return result

    rows, ranked, skipped = [], [], {}
    for event in events[:max(1, min(2000, int(max_events)))]:
        ticker = event.get("event_ticker") or event.get("ticker")
        if not ticker:
            continue
        markets = event.get("markets") or []
        event_row = {
            "event_ticker": ticker,
            "title": event.get("title") or event.get("sub_title") or "",
            "series_ticker": event.get("series_ticker"),
            "market_count": len(markets),
            "market_tickers": [m.get("ticker") for m in markets if m.get("ticker")][:50],
            "status": event.get("status"),
            "close_time": event.get("close_time"),
            "ranked_markets": [],
        }
        for market in markets:
            mticker = market.get("ticker")
            legs = market.get("mve_selected_legs") or []
            # A stale parent event can briefly contain a settled child market.
            # Never create or refresh a forecast once the exchange exposes outcome.
            if market.get("result") in ("yes", "no"):
                skipped["already_settled"] = skipped.get("already_settled", 0) + 1
                continue
            if market.get("status") not in (None, "", "active", "open"):
                skipped["market_not_open"] = skipped.get("market_not_open", 0) + 1
                continue
            if not mticker or not legs:
                continue
            if _excluded(event, market, legs):
                skipped["excluded_sport"] = skipped.get("excluded_sport", 0) + 1
                continue
            if len(legs) > max_legs or len(legs) < min_legs:
                skipped["leg_count_out_of_range"] = skipped.get("leg_count_out_of_range", 0) + 1
                continue
            leg_events = [leg.get("event_ticker") for leg in legs]
            if any(not x for x in leg_events) or len(set(leg_events)) != len(leg_events):
                skipped["correlated_or_unknown_event_legs"] = skipped.get("correlated_or_unknown_event_legs", 0) + 1
                continue

            leg_details, probabilities = [], []
            missing = False
            for leg in legs:
                lticker = leg.get("market_ticker")
                forecast = _leg_forecast(store, lticker, now, max_age_s) if lticker else None
                if not forecast:
                    missing = True
                    break
                side = str(leg.get("side", "yes")).lower()
                if side not in ("yes", "no"):
                    missing = True
                    break
                # Calibrate first, then shrink toward 50% when confidence or
                # independent signal diversity is weak.
                calibrated_yes = brain.calibrate(forecast["prob_yes"], store, "sports")
                p_selected = calibrated_yes if side == "yes" else 1 - calibrated_yes
                conservative_p = conservative_leg_probability(
                    p_selected, forecast["confidence"], forecast["external_signal_count"])
                probabilities.append(max(0.001, min(0.999, conservative_p)))
                leg_details.append({
                    "market_ticker": lticker, "event_ticker": leg["event_ticker"],
                    "side": side, "raw_probability": round(p_selected, 4),
                    "probability": round(conservative_p, 4),
                    "probability_shrinkage": round(abs(p_selected - conservative_p), 4),
                    "external_signal_count": forecast["external_signal_count"],
                    "confidence": round(forecast["confidence"], 3),
                    "signals": forecast["signals"], "latest_ts": forecast["latest_ts"],
                })
            if missing:
                skipped["missing_fresh_external_forecast"] = skipped.get("missing_fresh_external_forecast", 0) + 1
                continue

            yes_ask, no_ask = _market_ask(market, "yes"), _market_ask(market, "no")
            scored = score_combo_probabilities(probabilities, yes_ask, no_ask, fee_rate, min_edge)
            min_confidence = min((x["confidence"] for x in leg_details), default=0.0)
            mean_confidence = sum(x["confidence"] for x in leg_details) / len(leg_details)
            min_signal_count = min((x["external_signal_count"] for x in leg_details), default=0)
            # Three-to-five-leg combos are preferred; 6-8 legs must earn their
            # way back into the top ranks through strong evidence and EV.
            best = scored.get("best") or {}
            preference = combo_ranking_score(
                len(legs), best.get("expected_profit_per_contract", 0),
                min_confidence, min_edge)
            item = {
                "ticker": mticker, "title": market.get("title") or market.get("subtitle") or "",
                "leg_count": len(legs), "legs": leg_details,
                "minimum_leg_confidence": round(min_confidence, 3),
                "mean_leg_confidence": round(mean_confidence, 3),
                "minimum_external_signal_count": min_signal_count,
                **preference,
                "yes_ask": yes_ask, "no_ask": no_ask, **scored,
                "status": market.get("status"),
                "close_time": market.get("close_time"),
                "execution_enabled": False,
            }
            # Keep one auditable observation per combo ticker. On each open-market
            # scan, the latest pre-settlement forecast replaces the prior snapshot.
            # Once graded, Store refuses to overwrite it.
            best = item.get("best") or {}
            yes_mid = None
            yb = _price(market.get("yes_bid_dollars") or market.get("yes_bid"))
            ya = _price(market.get("yes_ask_dollars") or market.get("yes_ask"))
            if yb is not None and ya is not None and yb <= ya:
                yes_mid = round((yb + ya) / 2, 4)
            # Grade the conservative probability, not the raw independence product.
            predicted_yes = conservative_yes_probability(scored)
            audit_close_time = market.get("close_time") or event.get("close_time")
            audit_lead_seconds = int(getattr(settings, "combo_audit_lead_seconds", 300))
            audit_cutoff = combo_audit_cutoff_ts(audit_close_time, audit_lead_seconds)
            item["audit_snapshot_allowed"] = bool(audit_cutoff is not None and now < audit_cutoff)
            item["audit_cutoff_ts"] = audit_cutoff
            if item["audit_snapshot_allowed"]:
                store.record_combo_prediction({
                    "ticker": mticker, "ts": now, "event_ticker": ticker,
                    "leg_count": len(legs), "side": best.get("side"),
                    "market_price": best.get("ask"), "predicted_yes": predicted_yes,
                    "predicted_side_prob": best.get("conservative_win_probability"),
                    "market_yes_proxy": yes_mid, "fee": best.get("fee_per_contract"),
                    "expected_profit": best.get("expected_profit_per_contract"),
                    "eligible": item.get("eligible"), "ranking_score": item.get("ranking_score"),
                    "legs": leg_details,
                })
            else:
                reason = ("missing_close_time_for_audit" if audit_cutoff is None
                          else "inside_combo_audit_lead_time")
                skipped[reason] = skipped.get(reason, 0) + 1
            event_row["ranked_markets"].append(item)
            if item.get("best"):
                ranked.append({**item, "event_ticker": ticker, "event_title": event_row["title"]})
        if event_row["ranked_markets"] or event_row["market_count"]:
            rows.append(event_row)

    ranked.sort(key=lambda x: (
        bool(x.get("eligible")),
        x.get("ranking_score", -1e9),
        x.get("best", {}).get("expected_roi_on_cost", -1e9) or -1e9,
        x.get("best", {}).get("expected_profit_per_contract", -1e9),
    ), reverse=True)
    result = {
        "enabled": True, "ok": True, "ts": now, "count": len(rows),
        "min_legs": min_legs, "max_legs": max_legs,
        "scanned_markets": sum(len(e["market_tickers"]) for e in rows),
        "ranked_count": len(ranked), "top_candidates": ranked[:25],
        "events": rows, "skipped": skipped, "execution_enabled": False,
        "model": "historically calibrated, confidence-and-signal-diversity-shrunk leg probabilities; conservative joint haircut; 3-5-leg preference",
        "learning": {"table": "combo_prediction_audit", "graded_by": "actual Kalshi settlement",
                     "summary_by_leg_count": store.combo_prediction_summary(),
                     "note": "one latest snapshot per combo ticker; hypothetical P&L is not realized"},
        "reason": "Scans all open multivariate markets returned within the bounded API batch, then ranks every qualifying 3-8-leg candidate. Three-to-five legs are preferred; 6-8 legs avoid the length penalty only with >=85% minimum leg confidence and exceptional fee-adjusted EV. Leg forecasts are calibrated where enough historical data exists and shrunk toward 50% based on confidence and distinct external signal count. This is still a heuristic, not a validated joint-probability model. API coverage, freshness, correlation and outcome uncertainty remain limitations. Research only; no combo order is placed.",
    }
    store.set_kv("combo_research_health", result)
    return result
