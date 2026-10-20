"""AI briefing: every few hours a free LLM reads the bot's own results and
writes a plain-English note — what's working, what isn't, what to watch.

It's for YOU to read on your phone. It never changes settings or trades by
itself; the learning brain does the actual reweighting from hard numbers.
Every briefing is kept in the `lessons` table (append-only).
"""
import json
import os
import time

from . import brain
from .report import build

PROMPT = """You are the head of research for an automated prediction-market bot
that trades Kalshi sports games and 15-minute crypto windows.

Here is its performance report:
{report}

Its 20 most recently settled bets (signals at entry, result, P&L, CLV):
{bets}

Open positions right now:
{open_bets}

Recent signal-weight changes the bot made itself:
{weights}

Write a short briefing for the owner (max 180 words, plain English, no hype):
1. Is there evidence of a real edge yet? Say plainly if the sample is too small.
2. Which signals or leagues look strongest / weakest, and why that might be.
3. One or two concrete research ideas or settings to test next.
Never promise profits."""


def due(store, minutes=None):
    minutes = float(minutes or os.environ.get("BRIEFING_EVERY_MINUTES", 12))
    # small slack so a 12-minute schedule that starts a bit early still counts
    return time.time() - store.get_kv("briefing_last", 0) >= minutes * 60 - 90


def reserve():
    """AI calls kept back for the news panel and the council, which affect
    trades. The briefing only uses what's left over."""
    return int(os.environ.get("LLM_RESERVE_FOR_TRADING", 10))


def write(store, llm, path="data/BRIEFING.txt"):
    if llm is None:
        return None
    used = store.get_kv(llm._day_key(), 0)
    if llm.daily_limit - used <= reserve():
        store.set_kv("briefing_skipped", store.get_kv("briefing_skipped", 0) + 1)
        return None     # daily free AI quota nearly used: keep it for trading decisions
    bets = store.q("""SELECT series, team, side, price, result, pnl, clv, signals_json
                      FROM bets WHERE status='graded' ORDER BY graded_ts DESC LIMIT 20""")
    hist = store.q("SELECT signal, weight, reason FROM weight_history ORDER BY ts DESC LIMIT 15")
    open_bets = store.q("""SELECT ticker, side, price, contracts, edge FROM bets
                           WHERE status='open' ORDER BY ts DESC LIMIT 15""")
    prompt = PROMPT.format(report=build(store),
                           bets=json.dumps(bets, default=str)[:6000] if bets else "none settled yet",
                           open_bets=json.dumps(open_bets, default=str)[:2000] if open_bets else "none",
                           weights=json.dumps(hist)[:2000])
    text, model = llm.ask(prompt, temperature=0.3, max_tokens=500)
    store.set_kv("briefing_last", time.time())
    if not text:
        return None
    store.x("INSERT INTO lessons VALUES(?,?,?)", (int(time.time()), model, text))
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    with open(path, "w") as f:
        f.write(f"AI briefing ({model}, {stamp})\n\n{text}\n")
    return text
