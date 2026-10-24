"""The research council: free LLMs review shortlisted trades.

Roles: crypto analyst, liquidity analyst, probability check, risk controller,
chair (one call, one model), plus an INDEPENDENT skeptic on a different model.
Two calls per review keeps a free OpenRouter account workable.

Hard rules live in code, not in the prompt:
- The council only ever sees trades the numbers already approved.
- It can REJECT or WAIT, never approve something the numbers rejected,
  and never change the size.
- Stale or gappy data is an automatic reject before any LLM is asked.
- COUNCIL_MODE: "advisory" (default: veto power when available; numbers decide
  when the free quota is used up), "required" (no council = no trade), "off".
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

COUNCIL_PROMPT = """You are a trading council reviewing ONE proposed paper trade on a
Kalshi 15-minute crypto contract. Below is a compact numeric summary. Act as
each role in turn, then the chair decides. Be skeptical and brief.

{summary}

Roles:
- crypto_analyst: is price trending, ranging or unstable right now?
- liquidity_analyst: is the move backed by EXECUTED flow and persistent depth,
  or by walls that could be pulled?
- probability_check: is the model's probability plausible for THIS contract
  (strike, time left, averaging window)?
- risk_controller: is the edge big enough after fees and uncertainty?
- chair: "approve", "reject" or "wait" (wait = evidence may improve shortly).

Reply ONLY with JSON:
{{"crypto_analyst": "<short>", "liquidity_analyst": "<short>",
  "probability_check": "<short>", "risk_controller": "<short>",
  "chair": {{"decision": "approve|reject|wait", "reason": "<one sentence>"}}}}"""

SKEPTIC_PROMPT = """You are the skeptic on a trading council. Your only job is to find
reasons this trade is WRONG: stale or contradictory data, a move driven by
walls instead of trades, a probability that ignores the contract's averaging
window, or an edge too thin for the noise. Data:

{summary}

Reply ONLY with JSON: {{"severe_issue": true|false, "issues": ["<short>", ...]}}"""


def mode():
    return os.environ.get("COUNCIL_MODE", "advisory")



SPORTS_COUNCIL_PROMPT = """You are the risk-focused sports betting counsel reviewing a candidate
that has ALREADY passed deterministic pricing and risk checks. You may only approve,
reject, or wait; never increase stake or override hard risk limits. Check whether the
sport/event/team, evidence freshness, probability, market price, edge after fees,
and signal agreement are coherent. Missing evidence or unsupported claims should lead
to wait/reject, not confidence. Return JSON: {"decision":"approve|reject|wait",
"reason":"short evidence-based explanation"}.
Candidate: {summary}"""

SPORTS_SKEPTIC_PROMPT = """You are the independent skeptic reviewing a proposed sports
market candidate. Look for stale/incomplete data, mismatched event/team, lineup/injury
uncertainty, price/probability inconsistency, overconfident estimates, and hidden costs.
Do not invent facts. Reply ONLY JSON:
{"severe_issue":true|false,"issues":["short, concrete issue",...]}
Candidate: {summary}"""


def review_sports(llm, store, ticker, summary, cooldown_s=180):
    """Sports counsel handoff after deterministic risk checks; never changes sizing."""
    current_mode = mode()
    if current_mode == "off":
        return {"decision": "approve", "by": "numbers", "reason": "council off"}
    cached = store.get_kv(f"sports_council_{ticker}")
    if cached and time.time() - cached.get("ts", 0) < cooldown_s:
        return cached.get("result", {"decision": "wait", "by": "rules", "reason": "invalid cached review"})
    if llm is None or llm.budget_left() < 2:
        result = ({"decision": "wait", "by": "rules", "reason": "sports counsel required but unavailable"}
                  if current_mode == "required" else
                  {"decision": "approve", "by": "numbers", "reason": "counsel unavailable; deterministic checks only"})
        store.set_kv(f"sports_council_{ticker}", {"ts": time.time(), "result": result})
        return result
    text = json.dumps(summary, default=str)[:5000]
    models = [m for m in llm.models if m not in llm.dead]
    chair_model = models[0] if models else None
    skeptic_model = models[1] if len(models) > 1 else chair_model
    with ThreadPoolExecutor(max_workers=2) as pool:
        chair_future = pool.submit(llm.ask_json, SPORTS_COUNCIL_PROMPT.format(summary=text), chair_model)
        skeptic_future = pool.submit(llm.ask_json, SPORTS_SKEPTIC_PROMPT.format(summary=text), skeptic_model)
        chair, chair_used = chair_future.result()
        skeptic, skeptic_used = skeptic_future.result()
    decision = "wait"
    reasons = []
    if chair and isinstance(chair, dict):
        value = str(chair.get("decision", "")).lower()
        if value in ("approve", "reject", "wait"):
            decision = value
        reasons.append(str(chair.get("reason", ""))[:200])
    elif current_mode != "required":
        decision = "approve"
        reasons.append("counsel response unavailable; deterministic checks only")
    if skeptic and skeptic.get("severe_issue") is True:
        decision = "reject"
        reasons.extend(str(x)[:160] for x in skeptic.get("issues", [])[:3])
    result = {"decision": decision, "by": "council", "reason": " | ".join(x for x in reasons if x),
              "models": [m for m in (chair_used, skeptic_used) if m],
              "roles": chair or {}, "skeptic": skeptic or {}}
    store.set_kv(f"sports_council_{ticker}", {"ts": time.time(), "result": result})
    return result

def review(llm, store, ticker, summary, cooldown_s=180):
    """Return {"decision": approve|reject|wait, "by": council|rules|numbers, ...}."""
    dq = summary.get("spot", {}).get("data_quality", {})
    kb_age = summary.get("kalshi_book_age_s")
    if dq.get("stale") or (kb_age is not None and kb_age > 10):
        return {"decision": "reject", "by": "rules", "reason": "stale data"}
    if dq.get("exchanges_fresh", 0) < 1:
        return {"decision": "reject", "by": "rules", "reason": "no fresh spot price"}
    if mode() == "off":
        return {"decision": "approve", "by": "numbers", "reason": "council off"}

    cached = store.get_kv(f"council_{ticker}")
    if cached and time.time() - cached["ts"] < cooldown_s:
        return cached["result"]

    if llm is None or llm.budget_left() < 2:
        result = ({"decision": "wait", "by": "rules", "reason": "council required but unavailable"}
                  if mode() == "required" else
                  {"decision": "approve", "by": "numbers", "reason": "council unavailable; numbers only"})
        return result

    text = json.dumps(summary, default=str)[:5000]
    models = [m for m in llm.models if m not in llm.dead]
    chair_model = models[0] if models else None
    skeptic_model = models[1] if len(models) > 1 else chair_model
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(llm.ask_json, COUNCIL_PROMPT.format(summary=text), chair_model)
        f2 = pool.submit(llm.ask_json, SKEPTIC_PROMPT.format(summary=text), skeptic_model)
        council, cm = f1.result()
        skeptic, sm = f2.result()

    decision = "approve"
    reasons = []
    if council and isinstance(council.get("chair"), dict):
        d = str(council["chair"].get("decision", "")).lower()
        decision = d if d in ("approve", "reject", "wait") else "wait"
        reasons.append(str(council["chair"].get("reason", ""))[:200])
    elif mode() == "required":
        decision = "wait"
    if skeptic and skeptic.get("severe_issue") is True:
        decision = "reject"
        reasons.append("skeptic: " + "; ".join(map(str, skeptic.get("issues", [])))[:200])
    result = {"decision": decision, "by": "council", "reason": " | ".join(r for r in reasons if r),
              "models": [m for m in (cm, sm) if m], "roles": council or {}, "skeptic": skeptic or {}}
    store.set_kv(f"council_{ticker}", {"ts": time.time(), "result": result})
    return result
