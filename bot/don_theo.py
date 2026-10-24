"""Don Theo "Bones," The Rott Master: the fallback line bank and picker.

AI-written lines (OpenRouter / local AI) follow this same voice; when the AI
budget is out, these lines are used so he never goes quiet.

Rule he never breaks: bad news comes first, jokes second, and the real reason
is always returned alongside the line.
"""
import random

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
        "Easy money. I didn't even put my cigar down.",
        "Another bone for the collection.",
        "That's not luck, pal. That's a nose.",
    ],
    "win_solid": [
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
}

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
