"""Trade flow: reading the money on Kalshi's public (anonymous) trade feed.

We can't see WHO trades, but we can see HOW: big prints, sweeps that eat
several price levels at once, lopsided aggression, and price moves that happen
on Kalshi before the sportsbooks react.
"""
import math
from collections import defaultdict

from .base import Reading, clamp, logit, sigmoid


def flow_features(trades, big_threshold=None):
    """Summarise trades (dicts: ts, count, yes_price, taker_side)."""
    if not trades:
        return None
    counts = sorted(t["count"] for t in trades)
    if big_threshold is None:
        # "big" = top 10% of trade sizes in this window, at least 50 contracts
        big_threshold = max(50, counts[int(len(counts) * 0.9)] if len(counts) >= 10 else 50)

    yes_vol = sum(t["count"] for t in trades if t["taker_side"] == "yes")
    no_vol = sum(t["count"] for t in trades if t["taker_side"] == "no")
    big = [t for t in trades if t["count"] >= big_threshold]
    big_yes = sum(t["count"] for t in big if t["taker_side"] == "yes")
    big_no = sum(t["count"] for t in big if t["taker_side"] == "no")

    # sweep = 3+ same-side prints within one second at different prices
    by_sec = defaultdict(list)
    for t in trades:
        by_sec[(t["ts"], t["taker_side"])].append(t["yes_price"])
    sweeps = {"yes": 0, "no": 0}
    for (_, side), prices in by_sec.items():
        if len(prices) >= 3 and len(set(prices)) >= 2:
            sweeps[side] += 1

    total = yes_vol + no_vol
    first, last = trades[0]["yes_price"], trades[-1]["yes_price"]
    return {
        "n": len(trades), "volume": total,
        "imbalance": (yes_vol - no_vol) / total if total else 0.0,
        "big_imbalance": ((big_yes - big_no) / (big_yes + big_no)) if (big_yes + big_no) else 0.0,
        "big_count": len(big), "sweeps_yes": sweeps["yes"], "sweeps_no": sweeps["no"],
        "price_move": last - first, "big_threshold": big_threshold,
    }


def trade_flow(trades, kalshi_mid, book_move=None):
    """Lean the Kalshi mid toward where aggressive, large money is pushing.

    book_move: change in book fair prob over the same window. If Kalshi moved
    and the books didn't (yet), Kalshi flow may be early information.
    """
    f = flow_features(trades)
    if not f or kalshi_mid is None or f["volume"] < 20:
        return None
    sweep_bias = (f["sweeps_yes"] - f["sweeps_no"]) / max(1, f["sweeps_yes"] + f["sweeps_no"])
    pressure = 0.5 * f["big_imbalance"] + 0.3 * f["imbalance"] + 0.2 * sweep_bias
    lead = 0.0
    if book_move is not None and abs(f["price_move"]) > 0.02 and abs(book_move) < 0.01:
        lead = math.copysign(1.0, f["price_move"])    # Kalshi moved first
    shift = 0.25 * pressure + 0.15 * lead               # in log-odds
    est = sigmoid(logit(kalshi_mid) + shift)
    conf = clamp(math.log10(max(f["volume"], 1)) / 4 * (0.5 + 0.5 * abs(pressure)), 0.05, 1.0)
    f["kalshi_leads_books"] = bool(lead)
    return Reading("trade_flow", clamp(est), conf,
                   {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()})
