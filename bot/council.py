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
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("bot")

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

    # A failed/unavailable LLM must not turn every mathematically qualified setup
    # into an automatic no-trade. Cache the fallback for the same cooldown as an
    # AI vote, so a provider outage cannot trigger repeated orders every tick.
    def numeric_fallback(reason):
        result = {"decision": "approve", "by": "numbers", "reason": reason}
        store.set_kv(f"council_{ticker}", {"ts": time.time(), "result": result})
        log.warning("council %s fallback: %s", ticker, reason)
        return result

    if llm is None:
        return numeric_fallback(
            "LLM unavailable; numeric risk gate decides and rule-based council still votes")
    try:
        budget = llm.budget_left()
    except Exception:
        budget = 0
        log.exception("council %s could not read LLM budget", ticker)
    if budget < 2:
        return numeric_fallback(
            f"LLM budget unavailable (budget_left={budget}); numeric risk gate decides")

    text = json.dumps(summary, default=str)[:5000]
    models = [m for m in llm.models if m not in llm.dead]
    chair_model = models[0] if models else None
    skeptic_model = models[1] if len(models) > 1 else chair_model
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(llm.ask_json, COUNCIL_PROMPT.format(summary=text), chair_model)
            f2 = pool.submit(llm.ask_json, SKEPTIC_PROMPT.format(summary=text), skeptic_model)
            council, cm = f1.result()
            skeptic, sm = f2.result()
    except Exception as exc:
        reason = f"LLM review error ({type(exc).__name__}); numeric risk gate decides"
        log.exception("council %s model review failed", ticker)
        return numeric_fallback(reason)

    reasons = []
    valid_chair = bool(council and isinstance(council.get("chair"), dict)
                       and str(council["chair"].get("decision", "")).lower()
                       in ("approve", "reject", "wait"))
    if valid_chair:
        decision = str(council["chair"]["decision"]).lower()
        chair_reason = str(council["chair"].get("reason", "")).strip()[:200]
        if chair_reason:
            reasons.append(chair_reason)
    else:
        # Invalid/missing chair output is an unavailable vote, not a trade rejection.
        decision = "approve"
        reasons.append("chair response unavailable/invalid; numeric risk gate decides")
        log.warning("council %s fallback: invalid/missing chair JSON; chair_model=%s skeptic_model=%s",
                    ticker, cm, sm)
    if skeptic and skeptic.get("severe_issue") is True:
        decision = "reject"
        reasons.append("skeptic: " + "; ".join(map(str, skeptic.get("issues", [])))[:200])
    result = {"decision": decision, "by": "council" if valid_chair else "numbers",
              "reason": " | ".join(r for r in reasons if r),
              "models": [m for m in (cm, sm) if m], "roles": council or {}, "skeptic": skeptic or {}}
    log.info("council %s decision=%s by=%s chair_valid=%s skeptic_severe=%s reason=%s",
             ticker, result["decision"], result["by"], valid_chair,
             bool(skeptic and skeptic.get("severe_issue") is True), result["reason"][:300])
    store.set_kv(f"council_{ticker}", {"ts": time.time(), "result": result})
    return result


# --- The Council of 5 (+ Quant): rule-based member votes, graded, power earned ----
# Each member reads its own signals and votes back/pass on a candidate the math
# (risk.decide) already approved. Members start ADVISORY: votes are recorded and
# graded but change nothing. A member earns a veto, then a full vote, only from
# its graded record against the market (thresholds in config.Settings).
MEMBERS = {
    # member: (title, signals it reads, domain or None for both)
    "sharp": ("Sharp", ("book_consensus", "line_movement", "reverse_line_movement"), "sports"),
    "stats": ("Stats Analyst", ("ml_model",), None),
    "scout": ("Scout", ("ai_news",), "sports"),
    "situationist": ("Situationist", ("schedule_spot",), "sports"),
    "market": ("Market Reader", ("trade_flow", "spot_flow"), None),
    "quant": ("Quant", ("crypto_model", "crypto_model_iv", "crypto_momentum"), "crypto"),
}
STAGES = ("advisory", "veto", "full")


def _domain(series):
    return "crypto" if str(series or "").upper().endswith("15M") else "sports"


def member_votes(snap, plan, readings, prob=None, conf=None):
    """One vote per member that has data: back if its own probability for the
    chosen side beats the price after fees, else pass. Never invents data."""
    from . import fees
    side, price = plan.get("side"), plan.get("price")
    if side not in ("yes", "no") or not isinstance(price, (int, float)) or not 0 < price < 1:
        return []
    series = snap.get("series") or snap.get("ticker")
    domain = _domain(series)
    fee = fees.fee_per_contract(float(price), series=series)
    by_name = {r.name: r for r in readings}
    votes = []
    for member, (title, names, only) in MEMBERS.items():
        if only and only != domain:
            continue
        mine = [by_name[n] for n in names if n in by_name]
        if not mine and member == "stats" and isinstance(prob, (int, float)):
            # no trained model reading: the Stats Analyst reads the bot's own blend
            from .signals.base import Reading
            mine = [Reading("blend", float(prob), float(conf or 0.5))]
        weight = sum(max(r.confidence, 0.01) for r in mine)
        if not mine or weight <= 0:
            continue
        yes = sum(r.prob * max(r.confidence, 0.01) for r in mine) / weight
        mine_side = yes if side == "yes" else 1 - yes
        if not math.isfinite(mine_side):
            continue
        back = round(mine_side - price - fee, 9) > 0     # break-even after fees = pass
        lead = max(mine, key=lambda r: r.confidence).name.replace("_", " ")
        votes.append({"member": member, "vote": "back" if back else "pass",
                      "confidence": round(min(max(mine_side, 0.0), 1.0), 4),
                      "reason": f"{lead} puts {side.upper()} at {mine_side:.0%} vs "
                                f"{price * 100:.0f}c + {fee * 100:.1f}c fee"})
    return votes


def standings(store, settings, now=None):
    """Each member's graded record, power stage, doghouse flag and streak, in
    seating order (best recent record closest to the Boss, doghouse at the back)."""
    out = {}
    for member, (title, _, _) in MEMBERS.items():
        rows = store.q("""SELECT correct, brier, market_brier FROM council_votes
                          WHERE member=? AND outcome IS NOT NULL
                          ORDER BY settled_ts DESC, ts DESC LIMIT ?""",
                       (member, int(settings.council_power_window)))
        n = len(rows)
        scored = [r for r in rows if r["brier"] is not None and r["market_brier"] is not None]
        brier = sum(r["brier"] for r in scored) / len(scored) if scored else None
        market = sum(r["market_brier"] for r in scored) / len(scored) if scored else None
        recent = rows[:int(settings.council_doghouse_window)]
        recent_right = sum(r["correct"] for r in recent)
        streak = 0
        for r in rows:
            step = 1 if r["correct"] else -1
            if streak and (step > 0) != (streak > 0):
                break
            streak += step
        doghouse = (len(recent) >= int(settings.council_doghouse_window)
                    and recent_right <= int(settings.council_doghouse_max_right))
        stage = "advisory"
        if brier is not None and market is not None and not doghouse:
            if len(scored) >= settings.council_full_min_votes and \
                    brier <= market - settings.council_full_margin:
                stage = "full"
            elif len(scored) >= settings.council_veto_min_votes and brier < market:
                stage = "veto"
        last20 = rows[:20]
        out[member] = {"title": title, "graded": n,
                       "right": sum(r["correct"] for r in rows),
                       "recent": f"{sum(r['correct'] for r in last20)}-"
                                 f"{len(last20) - sum(r['correct'] for r in last20)}",
                       "recent_rate": (sum(r["correct"] for r in last20) / len(last20)) if last20 else None,
                       "last_window": f"{recent_right} for {len(recent)}",
                       "brier": None if brier is None else round(brier, 4),
                       "market_brier": None if market is None else round(market, 4),
                       "streak": streak, "doghouse": doghouse, "stage": stage}
    # members with no graded votes yet sit behind everyone with a record
    order = sorted(out, key=lambda m: (out[m]["doghouse"], out[m]["recent_rate"] is None,
                                       -(out[m]["recent_rate"] or 0),
                                       out[m]["brier"] if out[m]["brier"] is not None else 1.0))
    for seat, m in enumerate(order, 1):
        out[m]["seat"] = seat
    return out


def ruling(votes, table):
    """Advisory members never bind. Veto-stage members block only together; once
    a full-vote member sits, the empowered majority decides. The council can only
    PASS on a math-approved trade: it never creates one or changes its size."""
    empowered = [v for v in votes if table.get(v["member"], {}).get("stage") in ("veto", "full")]
    passes = [v for v in empowered if v["vote"] == "pass"]
    backs = sum(v["vote"] == "back" for v in votes)
    base = {"backs": backs, "passes": len(votes) - backs,
            "empowered": [v["member"] for v in empowered]}
    if not empowered:
        return {**base, "decision": "back", "binding": False, "reason": "advisory: the math decides"}
    full = any(table[v["member"]]["stage"] == "full" for v in empowered)
    block = len(passes) > len(empowered) / 2 if full else len(passes) == len(empowered)
    if block:
        who = ", ".join(table[v["member"]]["title"] for v in passes)
        return {**base, "decision": "pass", "binding": True,
                "reason": f"council veto ({who}): " + "; ".join(v["reason"] for v in passes)[:240]}
    return {**base, "decision": "back", "binding": True, "reason": "empowered members did not block"}


def convene(store, settings, snap, plan, readings, prob=None, conf=None, now=None):
    """Seat the members on one math-approved candidate: record their votes and
    return the ruling. Never raises into the trading path."""
    try:
        votes = member_votes(snap, plan, readings, prob, conf)
        if not votes:
            return {"decision": "back", "binding": False, "reason": "no member had data", "votes": []}
        store.record_council_votes(snap["ticker"], snap.get("series"), plan["side"],
                                   plan["price"], votes, ts=now)
        table = standings(store, settings, now)
        out = dict(ruling(votes, table), votes=votes)
        store.set_kv("members_last_ruling", {"ts": int(now or time.time()), "ticker": snap["ticker"],
                                      "side": plan["side"], "price": plan["price"], **out})
        return out
    except Exception as exc:                     # the council must never break trading
        log.warning("council convene %s: %s", snap.get("ticker"), exc)
        return {"decision": "back", "binding": False, "votes": [],
                "reason": f"council error: {type(exc).__name__}"}
