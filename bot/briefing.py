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
    from .llm import preferred_model
    text, model = llm.ask(prompt, model=preferred_model(llm, "briefing"),
                          temperature=0.3, max_tokens=500)
    store.set_kv("briefing_last", time.time())
    if not text:
        return None
    store.x("INSERT INTO lessons VALUES(?,?,?)", (int(time.time()), model, text))
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    with open(path, "w") as f:
        f.write(f"AI briefing ({model}, {stamp})\n\n{text}\n")
    return text


def sunday_sitdown(store, settings=None, llm=None, now=None, rng=None):
    """Weekly recap in Don Theo's voice, once per ISO week, on Sunday (UTC):
    record, P&L, best and worst call, the doghouse, who earned a better seat.
    Bad news first if the week lost money. Saved as kv sunday_sitdown."""
    import random
    from . import council, don_theo
    from .config import Settings
    now = int(now or time.time())
    if time.gmtime(now).tm_wday != 6:
        return None
    week = time.strftime("%G-W%V", time.gmtime(now))
    prev = store.get_kv("sunday_sitdown") or {}
    if prev.get("week") == week:
        return None
    settings = settings or Settings()
    rec = don_theo.record(store, getattr(settings, "mode", "paper"), now)
    table = council.standings(store, settings, now)
    nick = lambda m: don_theo.NICKNAMES.get(m, m)
    bad = [f"We're down ${-rec['pnl']:.2f} this week."] if rec["pnl"] < 0 else []
    parts = [f"Sunday Sit-Down. {rec['wins']}-{rec['losses']} this week, "
             f"{'+' if rec['pnl'] >= 0 else '-'}${abs(rec['pnl']):.2f}."]
    if rec["best"] and rec["bets"]:
        b = rec["best"]
        parts.append(f"Best call: {b['ticker']} {str(b['side']).upper()} at "
                     f"{b['price'] * 100:.0f}c, {b['pnl']:+.2f}.")
    if rec["worst"] and rec["bets"] and rec["worst"] is not rec["best"]:
        w = rec["worst"]
        parts.append(f"Worst call: {w['ticker']} {str(w['side']).upper()} at "
                     f"{w['price'] * 100:.0f}c, {w['pnl']:+.2f}.")
    graded = [m for m, r in table.items() if r["graded"]]
    top = min(graded, key=lambda m: table[m]["seat"]) if graded else None
    dog = [m for m in graded if table[m]["doghouse"]]
    if top and top not in dog:
        parts.append(f"{nick(top)} carried us ({table[top]['recent']} lately).")
    for m in dog:
        parts.append(don_theo.doghouse_line(m, tuple(map(int, table[m]["last_window"].split(" for ")))))
    old_seats = prev.get("seats") or {}
    moved_up = [m for m in graded if m in old_seats and table[m]["seat"] < old_seats[m]]
    if moved_up:
        parts.append("Earned a better seat: " + ", ".join(nick(m) for m in moved_up) + ".")
    if not graded:
        parts.append("No council votes graded yet. Everybody earns their kibble.")
    reason = " ".join(parts)
    closer = don_theo._ai_line(store, llm, reason, don_theo.SIGNATURE, now) or don_theo.SIGNATURE
    text = don_theo.first_bad_news(bad, reason + " " + closer)
    out = {"week": week, "ts": now, "text": text, "bad_news": bad,
           "seats": {m: r["seat"] for m, r in table.items()}}
    store.set_kv("sunday_sitdown", out)
    return out
