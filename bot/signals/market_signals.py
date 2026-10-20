"""Signals built from prices: Kalshi's own price, sportsbook consensus, and
betting trends (line movement, steam, reverse line movement)."""
import json
import os
import time

from .base import Reading, clamp, logit, sigmoid  # noqa: F401 (re-exported)


def kalshi_market(mkt):
    """The Kalshi price itself. Markets are usually close to right, so this is
    the anchor every other signal has to argue against."""
    bid, ask = mkt.get("yes_bid"), mkt.get("yes_ask")
    if bid is None or ask is None or ask <= 0:
        return None
    mid = (bid + ask) / 2
    spread = ask - bid
    # wide spreads = thin, less trustworthy price
    conf = clamp(1.0 - spread * 5, 0.1, 1.0)
    return Reading("kalshi_market", clamp(mid), conf,
                   {"mid": round(mid, 4), "spread": round(spread, 4)})


def book_consensus(fair_prob, n_books, book_spread):
    """Vig-free, sharp-weighted sportsbook probability."""
    if fair_prob is None:
        return None
    # more books and tighter agreement = more confidence
    conf = clamp(min(1.0, n_books / 8) * (1.0 - min(book_spread, 0.15) / 0.15 * 0.5),
                 0.1, 1.0)
    return Reading("book_consensus", clamp(fair_prob), conf,
                   {"n_books": n_books, "book_spread": round(book_spread, 4)})


def line_movement(history, now=None):
    """How the fair line moved from first sighting to now.

    Late, sharp moves often keep going until close (steam), so the signal leans
    a little further in the direction of travel. The learning brain decides
    whether that lean actually earns its weight.
    """
    if len(history) < 2:
        return None
    now = now or time.time()
    first, last = history[0]["fair_prob"], history[-1]["fair_prob"]
    move = logit(last) - logit(first)
    # recent window: moves in the last 3 hours count as "steam"
    recent = [h for h in history if h["ts"] >= now - 3 * 3600]
    steam = (logit(recent[-1]["fair_prob"]) - logit(recent[0]["fair_prob"])
             if len(recent) >= 2 else 0.0)
    est = sigmoid(logit(last) + 0.25 * move + 0.5 * steam)
    conf = clamp(min(abs(move) / 0.3, 1.0) * min(len(history) / 4, 1.0), 0.05, 1.0)
    return Reading("line_movement", clamp(est), conf, {
        "open": round(first, 4), "now": round(last, 4),
        "move_logit": round(move, 4), "steam_logit": round(steam, 4)})


def reverse_line_movement(team, history, public_pct):
    """Line moves toward a team that most public bets are AGAINST = sharp money.

    Needs public betting splits, which have no reliable free API. Drop a file at
    data/public_betting.json  {"Team Name": 0.31, ...}  (share of bets on that
    team) from any source you trust, and this signal switches on.
    """
    if public_pct is None or len(history) < 2:
        return None
    move = history[-1]["fair_prob"] - history[0]["fair_prob"]
    if move > 0.01 and public_pct < 0.45:       # moving toward the unpopular side
        strength = min(move / 0.05, 1.0) * (0.5 - public_pct) * 2
        est = history[-1]["fair_prob"] + 0.03 * strength
    elif move < -0.01 and public_pct > 0.55:    # moving away from the popular side
        strength = min(-move / 0.05, 1.0) * (public_pct - 0.5) * 2
        est = history[-1]["fair_prob"] - 0.03 * strength
    else:
        return None
    return Reading("reverse_line_movement", clamp(est), clamp(strength, 0.1, 1.0),
                   {"public_pct": public_pct, "line_move": round(move, 4)})


def load_public_betting(path="data/public_betting.json"):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
