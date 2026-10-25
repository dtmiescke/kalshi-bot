"""Don Theo "Bones," The Rott Master: the fallback line bank and picker.

AI-written lines (OpenRouter / local AI) follow this same voice; when the AI
budget is out, these lines are used so he never goes quiet.

Rule he never breaks: bad news comes first, jokes second, and the real reason
is always returned alongside the line.
"""
import os
import random
import time

NICKNAMES = {"sharp": "Numbers", "stats": "Calculator", "scout": "Eyes",
             "situationist": "Professor", "market": "Ticker", "quant": "Quant"}

LINES = {
    "agree": [
        "Now THAT'S a bone worth chewin'. Put the money down.",
        "Even I couldn't sniff out a hole in this one. We're in.",
        "The books are asleep at the wheel. Let's take their lunch money.",
        "Good boys. All of yous. Treats on me.",
    ],
    "disagree": [
        "Edge so thin I can see through it. Pass.",
        "The Rott Master don't chase scraps. Pass.",
        "Smells like a trap. And I got a very good nose.",
        "Not today. Not tomorrow. Get this outta my office.",
    ],
    "win_small": [
        "Like takin' a squeaky toy from a puppy.",
        "Easy money. I didn't even put my cigar down.",
        "Another bone for the collection.",
        "That's not luck, pal. That's a nose.",
    ],
    "win_solid": [
        "Told ya. Never doubt the nose.",
        "Did I stutter? Pay the dog.",
        "The sportsbooks need a better dog. Oh wait, they can't afford one.",
        "I called that before the coin was even flipped.",
    ],
    "win_big": [
        "They call me Bones 'cause I bury the competition.",
        "Frame that bet. Hang it in the lobby.",
        "Somebody get me a mirror. I wanna see what genius looks like.",
    ],
    "loss": [
        "Eh. Even the best dog misses a squirrel sometimes.",
        "Somebody's gettin' a talkin' to at the next meeting.",
    ],
    "loss_streak": ["Alright. Nobody say nothin'."],
    "chains": ["Real gold. Like my picks."],
    "opening": [
        "Sharp, go sniff out the line. Scout, I wanna know who's actually playin'.",
        "Everybody earns their kibble today. Get to work.",
    ],
    # one per mood, for the status line when nothing new settled
    "smooth": ["Smoke rings all around. Keep it comin'."],
    "all_business": ["Even week. Nobody's celebratin'. Sharpen up."],
    "grumpy": ["I want bigger edges or I want silence. Pick one."],
    "hot_hand": ["Somebody get me a bigger chair."],
    "best_day": ["Pour me somethin' nice."],
}
SIGNATURE = "Never doubt the nose."

STREAK_LINES = {3: "Three for three. I'm just warmin' up.",
                5: "Five straight. Somebody check if I'm legal.",
                7: "I'm not on a streak. I AM the streak."}


def mood(week_pnl, streak):
    """streak > 0 = consecutive wins, < 0 = consecutive losses."""
    if streak >= 5:
        return "hot_hand"
    if streak <= -3 or week_pnl < 0:
        return "grumpy"
    if week_pnl > 0:
        return "smooth"
    return "all_business"


def _pick(key, rng):
    return rng.choice(LINES[key])


def on_decision(approved, reason, rng=random):
    return {"line": _pick("agree" if approved else "disagree", rng), "reason": reason}


def on_settle(pnl, stake, streak, reason, doubters=(), rng=random):
    """Line for a settled bet. Gloating scales with the win; losses humble him."""
    if pnl < 0:
        key = "loss_streak" if streak <= -3 else "loss"
        return {"line": _pick(key, rng), "reason": reason}
    if streak >= 3:
        top = max(k for k in STREAK_LINES if streak >= k)
        line = STREAK_LINES[top]
    else:
        ret = pnl / stake if stake else 0
        key = "win_big" if ret >= 1.5 else "win_solid" if ret >= 0.5 else "win_small"
        line = _pick(key, rng)
    if doubters:
        who = NICKNAMES.get(doubters[0], doubters[0])
        line += f" {who}, you wanted to PASS on that? Sit in the corner."
    return {"line": line, "reason": reason}


def bad_news(message):
    """Problems are stated plainly first, then the attitude."""
    return {"line": f"{message} That's real. Nobody bets till it's fixed.", "reason": message}


def title(trouble):
    """What the Council calls him: "Rott Master" normally, "Don Theo" in trouble."""
    return "Don Theo" if trouble else "Rott Master"


def doghouse_line(member, last_window):
    """Cold-streak call-out, e.g. "Professor, you're 2 for your last 9." """
    right, n = last_window
    return (f"{NICKNAMES.get(member, member)}, you're {right} for your last {n}. "
            "You're on thin ice, pal.")


def first_bad_news(bad, line):
    """THE rule: problems are stated plainly before any joke. Always applied in
    code, whoever (AI or line bank) wrote `line`."""
    bad = [b.strip() for b in bad if b and b.strip()]
    if not bad:
        return line
    return " ".join(bad) + " That's real. Now let's figure out why. " + line


def record(store, mode="paper", now=None, days=7):
    """Real results he talks about: week P&L/record, best/worst call, streak."""
    now = int(now or time.time())
    week = store.q("""SELECT ticker, side, price, pnl FROM bets WHERE status='graded'
                      AND mode=? AND graded_ts>=? ORDER BY graded_ts""",
                   (mode, now - days * 86400))
    last = store.q("""SELECT pnl FROM bets WHERE status='graded' AND mode=?
                      ORDER BY graded_ts DESC, id DESC LIMIT 50""", (mode,))
    streak = 0
    for r in last:
        step = 1 if (r["pnl"] or 0) > 0 else -1
        if streak and (step > 0) != (streak > 0):
            break
        streak += step
    # rolling 24h blocks over the last 30 days: is the latest one the best?
    days_pnl = {r["d"]: r["pnl"] or 0 for r in store.q(
        """SELECT (? - graded_ts) / 86400 AS d, SUM(pnl) AS pnl FROM bets
           WHERE status='graded' AND mode=? AND graded_ts>? GROUP BY d""",
        (now, mode, now - 30 * 86400))}
    day = days_pnl.get(0, 0)
    return {"bets": len(week), "wins": sum((r["pnl"] or 0) > 0 for r in week),
            "losses": sum((r["pnl"] or 0) <= 0 for r in week),
            "pnl": round(sum(r["pnl"] or 0 for r in week), 2), "day_pnl": round(day or 0, 2),
            "best": max(week, key=lambda r: r["pnl"] or 0, default=None),
            "worst": min(week, key=lambda r: r["pnl"] or 0, default=None),
            "streak": streak,
            "best_day": day > 0 and all(day > v for d, v in days_pnl.items() if d != 0)}


def bad_news_items(store, rec, betting=(True, "ok")):
    """Plain statements of what's wrong right now: losses, pauses, sources down."""
    out = []
    if rec["pnl"] < 0:
        out.append(f"We're down ${-rec['pnl']:.2f} this week.")
    if not betting[0]:
        out.append(f"Betting's paused: {betting[1]}.")
    paused = store.get_kv("health_paused_areas") or {}
    for area in sorted(paused):
        out.append(f"{area.capitalize()} is benched: {paused[area]}.")
    return out


def _ai_line(store, llm, facts, fallback, now):
    """One short AI-written line in his voice from REAL facts, under a hard daily
    cap that leaves the trading reserve alone. None when unavailable."""
    if llm is None:
        return None
    from . import briefing
    day = "don_theo_ai_" + time.strftime("%Y%m%d", time.gmtime(now))
    cap = int(os.environ.get("DON_THEO_AI_PER_DAY", 6))
    used = store.get_kv(day, 0)
    if used >= cap or llm.budget_left() <= 0 or \
            llm.daily_limit - store.get_kv(llm._day_key(), 0) <= briefing.reserve():
        return None
    store.set_kv(day, used + 1)
    prompt = (
        "You are Don Theo \"Bones\" the Rott Master, a Rottweiler mob boss who runs a "
        "betting council. Mob-boss talk plus dog puns, short and punchy. Write ONE line "
        "(max 25 words) reacting to these REAL facts. Use only these numbers; never invent "
        "results. Do not repeat bad news, it is said before your line.\n"
        f"Facts: {facts}\nExample in his voice: {fallback}\nLine:")
    try:
        text, _ = llm.ask(prompt, temperature=0.8, max_tokens=80)
    except Exception:
        return None
    text = " ".join(str(text or "").split()).strip('"')
    return text if 0 < len(text) <= 220 else None


def speak(store, settings=None, llm=None, now=None, rng=random, betting=None):
    """His current line + mood, from real results. Bad news first, the real
    reason always attached, never blank. Saved as kv don_theo_last for the app."""
    from . import guard
    now = int(now or time.time())
    mode = getattr(settings, "mode", "paper")
    rec = record(store, mode, now)
    if betting is None:
        try:
            betting = guard.status(store, getattr(settings, "bankroll", 1000.0), mode=mode)
        except Exception as exc:
            betting = (False, f"guard check failed ({type(exc).__name__})")
    bad = bad_news_items(store, rec, betting)
    m = mood(rec["pnl"], rec["streak"])
    reason = (f"{rec['wins']}-{rec['losses']} this week, "
              f"{'+' if rec['pnl'] >= 0 else '-'}${abs(rec['pnl']):.2f}, streak {rec['streak']:+d}")
    if m == "hot_hand":
        key = m
    elif rec["best_day"] and not bad:
        key = "best_day"
    elif rec["bets"] == 0 and rec["streak"] == 0:
        key = "opening"
    else:
        key = m
    fallback = _pick(key, rng)
    if m == "hot_hand" and rec["streak"] >= 3:
        fallback = STREAK_LINES[max(k for k in STREAK_LINES if rec["streak"] >= k)] + " " + fallback
    line = _ai_line(store, llm, reason, fallback, now) or fallback
    by = "ai" if line != fallback else "line_bank"
    text = first_bad_news(bad, line).strip() or SIGNATURE      # never blank
    out = {"ts": now, "line": text, "reason": reason, "mood": m, "by": by,
           "title": title(bool(bad)), "bad_news": bad, "streak": rec["streak"],
           "week_pnl": rec["pnl"]}
    store.set_kv("don_theo_last", out)
    return out
