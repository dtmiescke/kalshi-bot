"""Sports-specific counsel for shortlisted candidates from the existing Engine.

Counsel may veto or wait; it can never create a candidate, change price, or increase size.
"""
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor


SPORTS_COUNSEL_PROMPT = """You are the skeptical sports betting counsel reviewing one already-qualified
Kalshi sports candidate. Do not invent injuries, lineups, scores, or facts not in the payload.
Look for stale sportsbook data, contradictory probabilities, weak evidence, and misleading edges.
You may only approve, reject, or wait. You cannot alter price, size, or risk limits.
Return ONLY JSON: {{"chair": {{"decision":"approve|reject|wait","reason":"short"}},
"skeptic": {{"severe_issue":true|false,"issues":["short"]}}}}

Candidate:
{payload}"""


def _reading_payload(readings):
    out = []
    for reading in readings:
        detail = getattr(reading, "detail", {}) or {}
        out.append({
            "name": getattr(reading, "name", ""),
            "prob": getattr(reading, "prob", None),
            "confidence": getattr(reading, "confidence", None),
            "detail": {k: v for k, v in detail.items()
                       if k in ("age_min", "n_books", "reason", "players",
                                "kalshi_leads_books", "spread", "source")},
        })
    return out


def review(llm, store, event, snap, plan, readings, prob, confidence, now=None):
    """Return a bounded counsel decision; hard data failures cannot be overridden."""
    now = int(now or time.time())
    ticker = str(snap.get("ticker") or "")
    result = {"decision": "wait", "by": "rules", "reason": "candidate context incomplete"}
    if not ticker or not event or not plan or not readings:
        return result
    if not isinstance(prob, (int, float)) or not math.isfinite(float(prob)) or not 0 < prob < 1:
        return {"decision": "reject", "by": "rules", "reason": "invalid model probability"}
    if not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)):
        return {"decision": "reject", "by": "rules", "reason": "invalid confidence"}
    if not any(getattr(r, "name", "") == "book_consensus" for r in readings):
        return {"decision": "reject", "by": "rules", "reason": "missing sportsbook consensus"}
    for reading in readings:
        age = (getattr(reading, "detail", {}) or {}).get("age_min")
        if age is not None and (not isinstance(age, (int, float)) or age > 90 or age < 0):
            return {"decision": "reject", "by": "rules", "reason": "stale or invalid odds evidence"}
    if not isinstance(plan.get("edge"), (int, float)) or not math.isfinite(float(plan["edge"])):
        return {"decision": "reject", "by": "rules", "reason": "invalid edge"}
    if plan.get("side") not in ("yes", "no") or not 0 < float(plan.get("price", 0)) < 1:
        return {"decision": "reject", "by": "rules", "reason": "invalid side or price"}

    mode = os.environ.get("COUNCIL_MODE", "advisory").strip().lower()
    if mode == "off":
        return {"decision": "approve", "by": "rules", "reason": "sports counsel disabled; numeric checks passed"}
    cache_key = f"sports_counsel_{ticker}"
    cached = store.get_kv(cache_key)
    if cached and now - int(cached.get("ts", 0)) < 180:
        return cached.get("result", result)
    payload = {
        "sport_series": snap.get("series"), "event_ticker": snap.get("event_ticker"),
        "event_title": event.get("title") or event.get("subtitle"),
        "market_ticker": ticker, "market_title": snap.get("title"),
        "team": snap.get("team"), "side": plan.get("side"), "price": plan.get("price"),
        "edge": plan.get("edge"), "model_probability": prob,
        "confidence": confidence, "signals": _reading_payload(readings),
        "note": "Only the supplied evidence is available; do not assume missing lineups or live stats.",
    }
    if llm is None or llm.budget_left() < 2:
        decision = ({"decision": "wait", "by": "rules", "reason": "required sports counsel unavailable"}
                   if mode == "required" else
                   {"decision": "approve", "by": "numbers", "reason": "numeric gates passed; AI counsel unavailable"})
        store.set_kv(cache_key, {"ts": now, "result": decision})
        return decision
    models = [m for m in llm.models if m not in llm.dead]
    model = models[0] if models else None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            answer, used = pool.submit(
                llm.ask_json, SPORTS_COUNSEL_PROMPT.format(
                    payload=json.dumps(payload, ensure_ascii=False, default=str)[:5000]), model
            ).result()
        chair = answer.get("chair", {}) if isinstance(answer, dict) else {}
        skeptic = answer.get("skeptic", {}) if isinstance(answer, dict) else {}
        choice = str(chair.get("decision", "")).lower()
        if choice not in ("approve", "reject", "wait"):
            choice = "wait" if mode == "required" else "approve"
        if skeptic.get("severe_issue") is True:
            choice = "reject"
        decision = {"decision": choice, "by": "sports_counsel" if answer else "numbers",
                    "reason": str(chair.get("reason") or "no counsel reason supplied")[:300],
                    "skeptic": skeptic if isinstance(skeptic, dict) else {},
                    "model": used}
    except Exception as exc:
        decision = ({"decision": "wait", "by": "rules", "reason": "sports counsel error"}
                    if mode == "required" else
                    {"decision": "approve", "by": "numbers",
                     "reason": f"numeric gates passed; counsel unavailable: {type(exc).__name__}"})
    store.set_kv(cache_key, {"ts": now, "result": decision})
    return decision
