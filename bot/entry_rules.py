"""Would waiting for confirmation have made entries better?

The bot bets the first time its edge clears the bar. This module replays the settled 15-minute windows
under a few alternative entry rules and scores each one: the first snapshot where the edge clears the
bar AND the rule's extra condition holds, bought at the ask plus a cent, fees included. Nothing here
places a bet or changes one; a rule only matters once it has enough windows and beats luck for the
number of rules compared (hindsight.family_z). Rules that need a closed candle (five minutes in or
more) pay Kalshi's later price, so the question is whether the extra information is worth more than
the price move it costs."""
import json
import math
import time

from . import entry_timing, hindsight, risk

MIN_TRADES = 60
RULES = ("first_clear", "away_from_even", "candle_5m", "candle_5m_vs_strike", "momentum_agree")
LABELS = {"first_clear": "Bet when the edge clears (current)",
          "away_from_even": "Skip prices near 50¢ (higher fees)",
          "candle_5m": "Wait for a 5-minute candle that agrees",
          "candle_5m_vs_strike": "…and price already on the right side of the strike",
          "momentum_agree": "5-min, 15-min and strike all agree, 8+ min in"}


def _sign(v):
    return 0 if v is None or v == 0 else (1 if v > 0 else -1)


def accepts(rule, yes, cost, min_left, ind):
    """Does this rule let the entry on `yes` (True for the YES side) through?"""
    want = 1 if yes else -1
    r5, r15, d = (_sign(ind.get(k)) for k in ("ret_5m", "ret_15m", "dist_sigma"))
    if rule == "first_clear":
        return True
    if rule == "away_from_even":
        return not 0.40 <= cost <= 0.60
    if rule == "candle_5m":
        return min_left <= 10.0 and r5 == want
    if rule == "candle_5m_vs_strike":
        return min_left <= 10.0 and r5 == want and d == want
    if rule == "momentum_agree":
        return min_left <= 7.0 and r5 == want and r15 == want and d == want
    return False


def _entry(rule, p, k, y, min_left, ind, fee_rate=0.07):
    for yes, win_p, cost in ((True, p, k + 0.01), (False, 1 - p, 1 - k + 0.01)):
        cost = min(cost, 0.99)
        fee = risk.taker_fee(100, cost, fee_rate) / 100
        if win_p - cost - fee >= entry_timing.EDGE_BAR and accepts(rule, yes, cost, min_left, ind):
            return (1 - cost - fee) if (y == 1) == yes else (-cost - fee)
    return None


def table(store, now=None):
    now = time.time() if now is None else now
    rows = store.q("""SELECT ticker, ts, model_prob, market_price, outcome, signals_json FROM decision_audit
                      WHERE decision='research' AND outcome IN (0,1) AND series LIKE 'KX%15M'
                      AND model_prob IS NOT NULL AND market_price BETWEEN ? AND ? AND ts >= ?
                      ORDER BY ts""", (*entry_timing.BAND, now - entry_timing.LOOKBACK_DAYS * 86400))
    taken = {r: {} for r in RULES}
    windows = set()
    for r in rows:
        close = entry_timing.close_ts(r["ticker"])
        if close is None:
            continue
        left = (close - r["ts"]) / 60.0
        if not 2.0 <= left <= entry_timing.BUCKETS[0][0]:
            continue
        windows.add(r["ticker"])
        try:
            ind = (json.loads(r["signals_json"] or "{}") or {}).get("indicators") or {}
        except ValueError:
            ind = {}
        for rule in RULES:
            if r["ticker"] in taken[rule]:
                continue
            pnl = _entry(rule, float(r["model_prob"]), float(r["market_price"]), int(r["outcome"]), left, ind)
            if pnl is not None:
                taken[rule][r["ticker"]] = pnl
    z = hindsight.family_z(len(RULES))
    out = []
    for rule in RULES:
        vals = list(taken[rule].values())
        n = len(vals)
        mean = sum(vals) / n if n else 0.0
        se = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1) / n) if n > 1 else 0.0
        lcb, ucb = mean - z * se, mean + z * se
        state = "learning" if n < MIN_TRADES else "paying" if lcb > 0 else "losing" if ucb < 0 else "no proof"
        out.append({"rule": rule, "label": LABELS[rule], "n": n, "mean": round(mean, 4), "lcb": round(lcb, 4),
                    "ucb": round(ucb, 4), "state": state})
    return {"rules": out, "windows": len(windows), "min_trades": MIN_TRADES, "needed_z": round(z, 2), "ts": int(now)}


def summary(store):
    return table(store)
