"""Do the hourly ladder and the 15-minute market that ends on the same hour agree?

A 15-minute "up" market struck at S0 and an hourly "above K" ladder rung settle on the same index at the same
instant. Because K above S0 can only be at most as likely, buying 15-minute YES while buying NO on the rung just
above S0 pays at least $1 whatever happens; the mirror holds for the rung at or below S0. If the two ever cost less
than $1 after both fees, that is a nearly risk-free trade with no forecast in it (the only risk is one leg not
filling). Earlier offline checks were fooled by quotes taken a minute apart, so this takes both at the same second:
one REST call returns every rung's quote, compared with the live 15-minute book. Nothing here places a bet."""
import statistics
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import risk

LADDER = {"KXBTC15M": "KXBTCD", "KXETH15M": "KXETHD"}
CHECK_EVERY = 30
MIN_LEFT = 2.0
MAX_LEFT = 14.0
KEEP_DAYS = 30
_ET = ZoneInfo("America/New_York")
SCHEMA = """CREATE TABLE IF NOT EXISTS cross_market (
    ticker TEXT, ts INTEGER, lead_min REAL, strike REAL, b15 REAL, a15 REAL,
    k_up REAL, bid_up REAL, ask_up REAL, k_dn REAL, bid_dn REAL, ask_dn REAL, net_up REAL, net_dn REAL,
    PRIMARY KEY (ticker, ts))"""


def event_ticker(series15, close_ts):
    """The hourly ladder event that closes with this 15-minute window, or None (window not on the hour)."""
    ladder = LADDER.get(series15)
    if not ladder or not close_ts or int(close_ts) % 3600:
        return None
    return f"{ladder}-{datetime.fromtimestamp(int(close_ts), _ET).strftime('%y%b%d%H').upper()}"


def rungs_from_markets(markets):
    """[(strike, yes_bid, yes_ask)] from Kalshi market objects, quoted rungs only, sorted by strike."""
    out = []
    for m in markets or ():
        try:
            k = float(m.get("floor_strike"))
            b = float(m.get("yes_bid_dollars") if m.get("yes_bid_dollars") not in (None, "") else m.get("yes_bid"))
            a = float(m.get("yes_ask_dollars") if m.get("yes_ask_dollars") not in (None, "") else m.get("yes_ask"))
        except (TypeError, ValueError):
            continue
        if a > 0 and b > 0 and a <= 1:           # a zero side is "no offer", not a price
            out.append((k, b, a))
    return sorted(out)


def _fee(p, rate):
    return risk.taker_fee(100, min(max(p, 0.01), 0.99), rate) / 100


def best_trades(strike, b15, a15, rungs, fee_rate=0.07):
    """Net profit per $1 of the two riskless pairings, after both taker fees, or None where a rung is missing.
    up: buy 15-minute YES at its ask and NO on the rung just above the strike.
    down: buy YES on the rung at or below the strike and 15-minute NO."""
    above = next(((k, b, a) for k, b, a in rungs if k > strike), None)
    below = next(((k, b, a) for k, b, a in reversed(rungs) if k <= strike), None)
    up = dn = None
    if above and a15 and a15 > 0:
        k, b, _ = above
        up = (k, b, above[2], (b - a15) - _fee(a15, fee_rate) - _fee(1 - b, fee_rate))
    if below and b15 and b15 > 0:
        k, b, a = below
        dn = (k, b, a, (b15 - a) - _fee(a, fee_rate) - _fee(1 - b15, fee_rate))
    return up, dn


class CrossMarket:
    def __init__(self, store):
        self.store = store
        self.next = {}          # ticker -> ts of the next allowed check

    def due(self, ticker, now, close_ts):
        lead = (close_ts - now) / 60.0
        return MIN_LEFT <= lead <= MAX_LEFT and now >= self.next.get(ticker, 0)

    def check(self, ticker, series, now, close_ts, strike, b15, a15, fetch_markets):
        """One same-second comparison. fetch_markets(event_ticker) -> Kalshi market dicts. Never raises."""
        try:
            self.next[ticker] = now + CHECK_EVERY
            ev = event_ticker(series, close_ts)
            if not ev or strike is None or b15 is None or a15 is None:
                return None
            rungs = rungs_from_markets(fetch_markets(ev))
            up, dn = best_trades(float(strike), float(b15), float(a15), rungs)
            if up is None and dn is None:
                return None
            self.store.x(SCHEMA)
            self.store.x("INSERT OR IGNORE INTO cross_market VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ticker, int(now), round((close_ts - now) / 60.0, 2), float(strike), float(b15), float(a15),
                          up and up[0], up and up[1], up and up[2], dn and dn[0], dn and dn[1], dn and dn[2],
                          up and round(up[3], 4), dn and round(dn[3], 4)))
            return up, dn
        except Exception:
            return None


def report(store, now=None):
    """What the same-second checks say. Safe on an empty table. Money is per $100 of each leg pair (x100)."""
    store.x(SCHEMA)
    now = time.time() if now is None else now
    store.x("DELETE FROM cross_market WHERE ts < ?", (int(now - KEEP_DAYS * 86400),))
    rows = store.q("SELECT * FROM cross_market")
    nets = [max(v for v in (r["net_up"], r["net_dn"]) if v is not None) for r in rows
            if r["net_up"] is not None or r["net_dn"] is not None]
    pos = [n for n in nets if n > 0]
    return {"checks": len(rows), "pairs": len(nets), "riskless": len(pos),
            "best": round(max(nets) * 100, 2) if nets else None,
            "median": round(statistics.median(nets) * 100, 2) if nets else None,
            "mean_riskless": round(sum(pos) / len(pos) * 100, 2) if pos else None}
