"""When in the 15-minute window have entries paid?

The bot may bet any time from 90 seconds in until two minutes before the close. It takes the first
moment its edge clears the bar. This module asks, from settled windows, whether some stretches of
the window do worse than others, and blocks a stretch only when the evidence says entries there
lose money. Until a stretch has enough windows it is left open, so by default nothing changes.

Each settled window contributes at most one would-be entry per stretch: the first 30-second
research snapshot in it where the forecast beat Kalshi's price by the edge bar after fees, bought
at the ask. The forecast used is the unshrunk one stored in the audit, so this measures the
population the bot's edge comes from. The result is a lower and upper bound on profit per contract,
with the bar raised for the number of stretches compared (hindsight.family_z)."""
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import hindsight, risk

BUCKETS = ((13.55, 12.0), (12.0, 10.0), (10.0, 8.0), (8.0, 6.0), (6.0, 4.0), (4.0, 2.0))   # minutes left, newest first
MIN_WINDOWS = 60
EDGE_BAR = 0.05
BAND = (0.10, 0.90)
LOOKBACK_DAYS = 30
_MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
_ET = ZoneInfo("America/New_York")


def close_ts(ticker):
    """Close time from a 15-minute ticker like KXBTC15M-26OCT282345-45 (the stamp is Eastern time)."""
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    yy, mon, dd, hh, mm = m.groups()
    return datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mm), tzinfo=_ET).timestamp()


def bucket_index(min_left):
    for i, (hi, lo) in enumerate(BUCKETS):
        if lo <= min_left <= hi:
            return i
    return None


def _entry_pnl(p, k, y, fee_rate=0.07):
    """Profit per $1 contract of the first side whose forecast edge clears the bar, else None."""
    for side, win_p, cost in (("yes", p, k + 0.01), ("no", 1 - p, 1 - k + 0.01)):
        cost = min(cost, 0.99)
        fee = risk.taker_fee(100, cost, fee_rate) / 100
        if win_p - cost - fee >= EDGE_BAR:
            won = (y == 1) == (side == "yes")
            return (1 - cost - fee) if won else (-cost - fee)
    return None


def table(store, now=None):
    """{"buckets": [{"from", "to", "n", "mean", "lcb", "ucb", "state"}], "windows": N}."""
    now = time.time() if now is None else now
    rows = store.q("""SELECT ticker, ts, model_prob, market_price, outcome FROM decision_audit
                      WHERE decision='research' AND brier IS NOT NULL AND outcome IN (0,1)
                      AND series LIKE 'KX%15M' AND model_prob IS NOT NULL AND market_price BETWEEN ? AND ? AND ts >= ?
                      ORDER BY ts""", (*BAND, now - LOOKBACK_DAYS * 86400))
    taken = {}                                   # (ticker, bucket) -> pnl: the first qualifying snapshot
    windows = set()
    for r in rows:
        close = close_ts(r["ticker"])
        if close is None:
            continue
        b = bucket_index((close - r["ts"]) / 60.0)
        if b is None or (r["ticker"], b) in taken:
            continue
        windows.add(r["ticker"])
        pnl = _entry_pnl(float(r["model_prob"]), float(r["market_price"]), int(r["outcome"]))
        if pnl is not None:
            taken[(r["ticker"], b)] = pnl
    z = hindsight.family_z(len(BUCKETS))
    out = []
    for i, (hi, lo) in enumerate(BUCKETS):
        vals = [v for (_, b), v in taken.items() if b == i]
        n = len(vals)
        mean = sum(vals) / n if n else 0.0
        se = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1) / n) if n > 1 else 0.0
        lcb, ucb = mean - z * se, mean + z * se
        state = ("learning" if n < MIN_WINDOWS else "blocked" if ucb < 0 else "favoured" if lcb > 0 else "open")
        out.append({"from": hi, "to": lo, "n": n, "mean": round(mean, 4), "lcb": round(lcb, 4),
                    "ucb": round(ucb, 4), "state": state})
    return {"buckets": out, "windows": len(windows), "ts": int(now)}


def allowed(tbl, min_left):
    """False only for a stretch of the window whose entries have clearly lost money."""
    if not tbl:
        return True
    i = bucket_index(min_left)
    return i is None or tbl["buckets"][i]["state"] != "blocked"


def summary(store):
    """For the app."""
    t = table(store)
    return {"windows": t["windows"], "min_windows": MIN_WINDOWS, "edge_bar": EDGE_BAR, "buckets": t["buckets"]}
