"""Does Kalshi's price lag the spot market, and is the lag tradable?

Forecasting from candles has not beaten Kalshi's own price (see the "Is the edge real?" card). The one
edge a fast bot can plausibly have is speed: when spot jumps, Kalshi's quotes may take seconds to move,
and for that moment the old price is stale. This records the evidence instead of assuming it.

A jump is a move of the composite spot over the last 5 seconds of at least JUMP_Z standard deviations
(from the bot's own volatility forecast). For each jump we keep the Kalshi quote at the jump, then the
mid and the formula probability at 2, 5, 10, 20, 30 and 60 seconds, and when the window settles the
profit of buying the jump's direction at the ask quoted when it was spotted, plus half a cent, held to settlement. report() then answers two
questions: how much of the formula's move had Kalshi's mid made by each horizon (the slope), and
whether that trade made money after fees (mean, z), both bought the instant the jump is seen and bought
LATENCY_S seconds later at the quote then, which is what an order could actually get. Nothing here places a bet."""
import json
import math
import time
from collections import deque

from . import risk

HORIZONS = (2, 5, 10, 20, 30, 60)
LATENCY_S = 2          # a real order is not filled the instant a jump is seen; the realistic entry is this many seconds later
JUMP_Z = 2.0
LOOK_S = 5
COOLDOWN_S = 90
KEEP_DAYS = 30
SCHEMA = """CREATE TABLE IF NOT EXISTS {table} (
    ticker TEXT, series TEXT, ts INTEGER, direction INTEGER, z REAL, spot0 REAL, bid0 REAL, ask0 REAL,
    model0 REAL, follow TEXT, PRIMARY KEY (ticker, ts))"""


def ensure_table(store, table="lead_lag"):
    store.x(SCHEMA.format(table=table))


class LeadLag:
    """table="perp_lead" watches the perpetual-futures mid instead of the spot composite: `price` passed to observe()
    is then the perp mid and `ref` the spot composite, so each jump records whether spot had already moved."""

    def __init__(self, store, jump_z=JUMP_Z, table="lead_lag"):
        self.store, self.jump_z, self.table = store, jump_z, table
        self.ref_hist = {}   # ticker -> deque[(ts, reference spot)]
        self.hist = {}       # ticker -> deque[(ts, spot)]
        self.last = {}       # ticker -> ts of the last jump
        self.pending = []    # jumps still collecting follow-ups

    def observe(self, ticker, series, now, spot, bid, ask, model_prob, sigma_per_min, ref=None):
        """Call once a second per market. Never raises into the trading loop."""
        try:
            self._observe(ticker, series, now, spot, bid, ask, model_prob, sigma_per_min, ref)
        except Exception:      # a study must never stop a bet
            pass

    def _observe(self, ticker, series, now, spot, bid, ask, model_prob, sigma_per_min, ref=None):
        if not spot or bid is None or ask is None or model_prob is None:
            return
        h = self.hist.setdefault(ticker, deque())
        h.append((now, spot))
        while h and now - h[0][0] > LOOK_S + 6:
            h.popleft()
        if ref:
            rh = self.ref_hist.setdefault(ticker, deque())
            rh.append((now, ref))
            while rh and now - rh[0][0] > LOOK_S + 6:
                rh.popleft()
        mid = (float(bid) + float(ask)) / 2
        for ev in [e for e in self.pending if e["ticker"] == ticker]:
            for s in HORIZONS:
                if s not in ev["follow"] and now - ev["ts"] >= s:
                    ev["follow"][s] = [round(mid, 4), round(float(model_prob), 4), round(float(bid), 4), round(float(ask), 4)]
            if len(ev["follow"]) == len(HORIZONS) or now - ev["ts"] > HORIZONS[-1] + 30:
                self._store(ev)
                self.pending.remove(ev)
        if not sigma_per_min or now - self.last.get(ticker, 0) < COOLDOWN_S:
            return
        old = next((p for t, p in h if now - t >= LOOK_S - 0.5), None)
        if not old:
            return
        z = math.log(spot / old) / (float(sigma_per_min) * math.sqrt(LOOK_S / 60.0))
        if abs(z) >= self.jump_z:
            self.last[ticker] = now
            ref_z = None
            if ref and sigma_per_min:       # had the reference (spot) already moved over the same seconds?
                old_ref = next((p for t, p in self.ref_hist.get(ticker, ()) if now - t >= LOOK_S - 0.5), None)
                if old_ref:
                    ref_z = round(math.log(ref / old_ref) / (float(sigma_per_min) * math.sqrt(LOOK_S / 60.0)), 2)
            self.pending.append({"ticker": ticker, "series": series, "ts": int(now), "direction": 1 if z > 0 else -1,
                                 "z": round(z, 2), "spot0": spot, "bid0": float(bid), "ask0": float(ask),
                                 "model0": float(model_prob), "follow": {}, "ref_z": ref_z})

    def _store(self, ev):
        ensure_table(self.store, self.table)
        follow = {str(k): v for k, v in ev["follow"].items()}
        if ev.get("ref_z") is not None:
            follow["ref_z"] = ev["ref_z"]
        self.store.x(f"INSERT OR IGNORE INTO {self.table} VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (ev["ticker"], ev["series"], ev["ts"], ev["direction"], ev["z"], ev["spot0"], ev["bid0"],
                      ev["ask0"], ev["model0"], json.dumps(follow)))


def _slope(pairs):
    """Least squares through the origin of y on x, with its standard error."""
    sxx = sum(x * x for x, _ in pairs)
    if sxx <= 0 or len(pairs) < 5:
        return None, None
    b = sum(x * y for x, y in pairs) / sxx
    resid = sum((y - b * x) ** 2 for x, y in pairs) / (len(pairs) - 1)
    return b, math.sqrt(resid / sxx)


LEAD_REF_Z = 1.0       # the reference (spot) counts as "not yet moved" below this many standard deviations


def _ref_z(row):
    v = json.loads(row["follow"] or "{}").get("ref_z")
    return None if v is None else float(v)


def report(store, now=None, fee_rate=0.07, table="lead_lag"):
    """What the recorded jumps say. Safe on an empty table. For the perp study, "unmoved" repeats the numbers for
    the jumps where spot had NOT already moved: those are the ones where the perp really came first."""
    ensure_table(store, table)
    now = time.time() if now is None else now
    store.x(f"DELETE FROM {table} WHERE ts < ?", (int(now - KEEP_DAYS * 86400),))
    rows = store.q(f"""SELECT l.*, g.outcome FROM {table} l
                       LEFT JOIN graded_markets g ON g.ticker = l.ticker""")
    out = _summarize(rows, fee_rate)
    known = [r for r in rows if _ref_z(r) is not None]
    if known:
        out["unmoved"] = _summarize([r for r in known if abs(_ref_z(r)) < LEAD_REF_Z], fee_rate)
        out["unmoved"]["of"] = len(known)
    return out


def _summarize(rows, fee_rate=0.07):
    follow = {}
    for s in HORIZONS:
        pairs = []
        for r in rows:
            f = json.loads(r["follow"] or "{}").get(str(s))
            if f:
                mid0 = (r["bid0"] + r["ask0"]) / 2
                pairs.append((r["direction"] * (f[1] - mid0), r["direction"] * (f[0] - mid0)))
        b, se = _slope(pairs)
        follow[str(s)] = None if b is None else {"slope": round(b, 3), "se": round(se, 3), "n": len(pairs)}
    def trade(side_yes, ask, bid, outcome):
        cost = min((ask if side_yes else 1 - bid) + 0.005, 0.99)
        fee = risk.taker_fee(100, cost, fee_rate) / 100
        return (1 - cost - fee) if (outcome == 1) == side_yes else (-cost - fee)

    def stats(pnl):
        n = len(pnl)
        mean = sum(pnl) / n if n else None
        se = (math.sqrt(sum((v - mean) ** 2 for v in pnl) / (n - 1) / n) if n > 1 else None)
        return n, mean, (mean / se if se else None)

    instant, delayed = [], []
    for r in rows:
        if r["outcome"] not in (0, 1):
            continue
        up = r["direction"] > 0
        instant.append(trade(up, r["ask0"], r["bid0"], r["outcome"]))       # at the quote when the jump was spotted
        f = json.loads(r["follow"] or "{}").get(str(LATENCY_S))
        if f and len(f) >= 4:                                               # at the quote LATENCY_S seconds later
            delayed.append(trade(up, f[3], f[2], r["outcome"]))
    n, mean, z = stats(instant)
    dn, dmean, dz = stats(delayed)
    return {"events": len(rows), "settled": n, "mean_pnl": None if mean is None else round(mean, 4),
            "z": None if z is None else round(z, 2),
            "delayed": {"latency_s": LATENCY_S, "settled": dn, "mean_pnl": None if dmean is None else round(dmean, 4),
                        "z": None if dz is None else round(dz, 2)},
            "follow": follow, "jump_z": JUMP_Z}
