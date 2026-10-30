"""Do Kalshi's own sports prices agree with each other? Measured from Kalshi quotes alone, nothing placed.

Some sets of Kalshi markets are tied together by arithmetic, so no forecast is needed to spot a bad price:

* winner: the outcomes of one game are exclusive and exhaustive, so buying YES on every outcome pays exactly $1;
  buying NO on every outcome pays exactly n-1 dollars. Either costs less than that only if the book is crossed.
* ladder: "20+ points" can never be likelier than "15+ points" for the same player (same for totals). Buying YES on
  the lower rung and NO on the higher rung pays at least $1 whatever happens, so it is a mispricing whenever the higher
  rung's bid is above the lower rung's ask.

Each is priced at the real asks and bids with both taker fees. A third, softer check fits a smooth non-increasing curve
to each ladder's mid prices (pool-adjacent-violators) and records how far each rung sits from it; that is not riskless,
it is stored so a later study can ask whether rungs off their own curve settle differently from their price.

Every call is one REST read of open events; the scanner only writes its own table and a few kv counters."""
import json
import logging
import os
import re
import statistics
import time

from .cross_market import _fee

log = logging.getLogger(__name__)

SERIES = ("KXNBAGAME", "KXNBATOTAL", "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA",
          "KXNHLGAME", "KXNFLGAME", "KXMLBGAME")
KEEP_DAYS = 30
CURVE_EVERY = 1800        # curve snapshots (not riskless candidates) are kept at most this often, and capped per pass
CURVE_CAP = 150
NEAR_CAP = 300
NEAR = -0.03              # candidates this close to riskless are kept so the distance can be measured
CURVE_FLAG = 0.03         # a rung this far past half its own spread from its curve is recorded
SCHEMA = """CREATE TABLE IF NOT EXISTS sports_consistency (
    ts INTEGER, kind TEXT, series TEXT, event TEXT, net REAL, detail TEXT)"""
CURVE_SCHEMA = """CREATE TABLE IF NOT EXISTS sports_curve (
    ts INTEGER, ticker TEXT, event TEXT, strike REAL, bid REAL, ask REAL, mid REAL, fit REAL)"""
_PLAYER = re.compile(r"^(.*?):\s*[\d.]+\+?$")


def quote(m):
    """(bid, ask) in dollars for the YES side, or None unless both sides of the book exist."""
    try:
        bid = float(m.get("yes_bid_dollars") if m.get("yes_bid_dollars") not in (None, "") else m.get("yes_bid"))
        ask = float(m.get("yes_ask_dollars") if m.get("yes_ask_dollars") not in (None, "") else m.get("yes_ask"))
    except (TypeError, ValueError):
        return None
    if not (0 < bid <= ask < 1):
        return None                  # a zero side is "no offer", and a crossed book is bad data, not a trade
    return bid, ask


def open_markets(event):
    return [m for m in event.get("markets") or [] if m.get("status") in ("active", "open", None)]


def winner_check(event, fee_rate=0.07):
    """Net profit per $1 of the better of 'YES on every outcome' and 'NO on every outcome', or None."""
    ms = open_markets(event)
    if not 2 <= len(ms) <= 3:
        return None
    qs = [quote(m) for m in ms]
    if any(q is None for q in qs):
        return None
    n = len(qs)
    yes = 1 - sum(a + _fee(a, fee_rate) for _, a in qs)
    no = (n - 1) - sum((1 - b) + _fee(1 - b, fee_rate) for b, _ in qs)
    best = max(yes, no)
    return {"net": round(best, 4), "side": "yes" if yes >= no else "no", "n": n,
            "asks": [a for _, a in qs], "bids": [b for b, _ in qs]}


def ladders(event):
    """{key: [(strike, ticker, bid, ask)]} of rising-threshold 'greater' rungs, sorted by strike, quoted rungs only."""
    groups = {}
    for m in open_markets(event):
        if m.get("strike_type") not in ("greater", "greater_or_equal"):
            continue
        q = quote(m)
        try:
            k = float(m.get("floor_strike"))
        except (TypeError, ValueError):
            continue
        if q is None:
            continue
        who = _PLAYER.match(str(m.get("yes_sub_title") or ""))
        groups.setdefault(who.group(1).strip() if who else "", []).append((k, m.get("ticker"), q[0], q[1]))
    return {key: sorted(rungs) for key, rungs in groups.items() if len(rungs) >= 2}


def ladder_check(rungs, fee_rate=0.07):
    """Best monotonicity trade on one ladder: YES on a lower rung at its ask + NO on a higher rung at 1 - its bid.
    Returns {"net", "low", "high"} (net per $1, after both fees) or None."""
    best = None
    for i, (k1, t1, b1, a1) in enumerate(rungs):
        for k2, t2, b2, a2 in rungs[i + 1:]:
            if k2 <= k1:
                continue
            net = 1 - (a1 + _fee(a1, fee_rate)) - ((1 - b2) + _fee(1 - b2, fee_rate))
            if best is None or net > best["net"]:
                best = {"net": round(net, 4), "low": t1, "high": t2, "k_low": k1, "k_high": k2,
                        "ask_low": a1, "bid_high": b2}
    return best


def isotonic_fit(values, weights=None):
    """Non-increasing least-squares fit (pool adjacent violators). Same length as values."""
    w = list(weights or [1.0] * len(values))
    blocks = []                                           # [sum_wv, sum_w, count]
    for v, wt in zip(values, w):
        blocks.append([v * wt, wt, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            last = blocks.pop()
            blocks[-1][0] += last[0]
            blocks[-1][1] += last[1]
            blocks[-1][2] += last[2]
    out = []
    for s, wt, n in blocks:
        out.extend([s / wt] * n)
    return out


def curve_gaps(rungs):
    """[(ticker, strike, bid, ask, mid, fit)] for a ladder of at least 4 rungs: mid prices against their own
    non-increasing curve, weighting tight quotes more."""
    if len(rungs) < 4:
        return []
    mids = [(b + a) / 2 for _, _, b, a in rungs]
    fit = isotonic_fit(mids, [1.0 / max(a - b, 0.01) for _, _, b, a in rungs])
    return [(t, k, b, a, round(mid, 4), round(f, 4)) for (k, t, b, a), mid, f in zip(rungs, mids, fit)]


class Scanner:
    def __init__(self, store):
        self.store = store

    def scan_event(self, series, event, now, tally):
        """Check one event; returns candidate rows written. Never raises."""
        written = 0
        try:
            ev_ticker = event.get("event_ticker") or ""
            if series.endswith("GAME"):
                res = winner_check(event)
                if res:
                    written += self._note("winner", series, ev_ticker, res["net"], res, now, tally)
            for key, rungs in ladders(event).items():
                res = ladder_check(rungs)
                if res:
                    res["player"] = key
                    written += self._note("ladder", series, ev_ticker, res["net"], res, now, tally)
                for t, k, b, a, mid, fit in curve_gaps(rungs):
                    if tally.get("curve_on") and tally["curve_rows"] < CURVE_CAP and abs(mid - fit) > (a - b) / 2 + CURVE_FLAG:
                        tally["curve_rows"] += 1
                        self.store.x("INSERT INTO sports_curve VALUES(?,?,?,?,?,?,?,?)",
                                     (int(now), t, ev_ticker, k, b, a, mid, fit))
                        tally["curve_flags"] += 1
        except Exception:
            log.debug("sports consistency scan failed for %s", event.get("event_ticker"), exc_info=True)
        return written

    def _note(self, kind, series, ev_ticker, net, detail, now, tally):
        tally[f"{kind}_checks"] += 1
        tally["best"][kind] = max(tally["best"].get(kind, -9), net)
        if net > 0:
            tally["riskless"] += 1
        if net >= NEAR and tally["near_rows"] < NEAR_CAP:
            tally["near_rows"] += 1
            self.store.x("INSERT INTO sports_consistency VALUES(?,?,?,?,?,?)",
                         (int(now), kind, series, ev_ticker, net, json.dumps(detail, default=str)[:900]))
            return 1
        return 0


def run(store, client, now=None, every=300, series=None):
    """One throttled pass over the open events of each series. Never raises, never places anything."""
    now = int(now or time.time())
    try:
        if now - float(store.get_kv("sports_consistency_last", 0) or 0) < every:
            return None
        store.set_kv("sports_consistency_last", now)
        store.x(SCHEMA)
        store.x(CURVE_SCHEMA)
        names = series or [s for s in os.environ.get("SPORTS_CONSISTENCY_SERIES", ",".join(SERIES)).split(",") if s]
        curve_on = now - float(store.get_kv("sports_curve_last", 0) or 0) >= CURVE_EVERY
        if curve_on:
            store.set_kv("sports_curve_last", now)
        tally = {"winner_checks": 0, "ladder_checks": 0, "riskless": 0, "curve_flags": 0, "best": {}, "events": 0,
                 "curve_on": curve_on, "curve_rows": 0, "near_rows": 0}
        scanner = Scanner(store)
        for name in names:
            try:
                events = client._paged("/events", "events", {"series_ticker": name, "status": "open",
                                                             "with_nested_markets": "true", "limit": 200}, max_pages=2)
            except Exception as exc:
                log.warning("sports consistency: %s unavailable: %s", name, exc)
                continue
            for ev in events:
                tally["events"] += 1
                scanner.scan_event(name, ev, now, tally)
        total = store.get_kv("sports_consistency_total", {}) or {}
        for key in ("winner_checks", "ladder_checks", "riskless", "curve_flags", "events"):
            total[key] = total.get(key, 0) + tally[key]
        total["passes"] = total.get("passes", 0) + 1
        best = total.get("best", {})
        for kind, net in tally["best"].items():
            best[kind] = max(best.get(kind, -9), net)
        total["best"] = best
        store.set_kv("sports_consistency_total", total)
        return tally
    except Exception:
        log.warning("sports consistency pass failed", exc_info=True)
        return None


def report(store, now=None):
    """What the scans say. Safe on an empty database. Money is per $100 of contracts (x100)."""
    store.x(SCHEMA)
    store.x(CURVE_SCHEMA)
    now = time.time() if now is None else now
    cut = int(now - KEEP_DAYS * 86400)
    store.x("DELETE FROM sports_consistency WHERE ts < ?", (cut,))
    store.x("DELETE FROM sports_curve WHERE ts < ?", (cut,))
    total = store.get_kv("sports_consistency_total", {}) or {}
    rows = store.q("SELECT kind, net FROM sports_consistency")
    out = {"passes": total.get("passes", 0), "events": total.get("events", 0),
           "winner_checks": total.get("winner_checks", 0), "ladder_checks": total.get("ladder_checks", 0),
           "riskless": total.get("riskless", 0), "curve_flags": total.get("curve_flags", 0), "kinds": {}}
    for kind in ("winner", "ladder"):
        nets = [r["net"] for r in rows if r["kind"] == kind]
        out["kinds"][kind] = {
            "near": len(nets), "positive": sum(1 for n in nets if n > 0),
            "best": round((total.get("best", {}).get(kind)) * 100, 2) if kind in total.get("best", {}) else None,
            "median_near": round(statistics.median(nets) * 100, 2) if nets else None}
    return out
