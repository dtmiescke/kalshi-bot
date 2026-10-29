"""Experiment lane: what would the loosened rules have done at the real prices? A shadow account, not a trader.

The main bot restricts itself: it trusts only about a quarter of its own forecast, wants a 5% edge, skips proven
losing kinds of bet and stakes little. This lane asks whether those restrictions are steering it away from trades it
would profit from. It takes the opposite rules (full trust in the forecast, a lower edge bar, no proven-loser skips,
no confidence or long-shot guards) and records what each trade WOULD have cost, using the live order book at that
second: a depth-aware fill at the real ask plus the real taker fee, a fixed small stake.

It is deliberately separate from everything that matters:
- it keeps its own table (experiment_bets), never the real bets table, so it cannot reach the account, the risk
  caps, the window locks, the council, the learning, the proven-loser gate or the go-live checklist;
- it never places or simulates an order through the engine;
- a failure here can never stop a bet (observe is wrapped, like the other studies).
Results are in dollars per $100 staked. Replays of past readings gave about -$11 per $100 for the loosest rules, so
the expectation is that this lane loses to costs; running it live is how that gets confirmed or refuted with real
fills instead of assumed mid prices."""
import math
import statistics
import time

from . import risk

MIN_EDGE = 0.03
STAKE = 5.0
CHASE = 0.02            # never pay more than 2 cents above the best ask
MIN_PRICE, MAX_PRICE = 0.03, 0.97
SCHEMA = """CREATE TABLE IF NOT EXISTS experiment_bets (
    ticker TEXT PRIMARY KEY, ts INTEGER, series TEXT, side TEXT, price REAL, contracts INTEGER, stake REAL,
    fee REAL, p_win REAL, edge REAL, mins_left REAL, status TEXT, outcome INTEGER, pnl REAL, settled_ts INTEGER)"""


def ensure_table(store):
    store.x(SCHEMA)


def decide(prob_yes, asks, min_edge=MIN_EDGE, stake=STAKE, fee_rate=0.07):
    """The loosened rule on one reading. prob_yes: the full, unshrunk forecast. asks: {"yes": [(price, size)],
    "no": [...]} ascending. Returns the better side's fill {"side","price","contracts","fee","p_win","edge"} when its
    edge after the real fee clears the bar, else None. Fills walk the real depth, so size and slippage are honest."""
    if prob_yes is None or not asks:
        return None
    best = None
    for side, p_win in (("yes", prob_yes), ("no", 1 - prob_yes)):
        ladder = asks.get(side) or []
        if not ladder:
            continue
        limit = round(ladder[0][0] + CHASE, 4)
        contracts, avg = risk.fill_plan(ladder, stake, limit)
        if contracts < 1 or avg is None or not MIN_PRICE <= avg <= MAX_PRICE:
            continue
        fee = risk.taker_fee(contracts, avg, fee_rate)
        edge = p_win - avg - fee / contracts
        if edge >= min_edge and (best is None or edge > best["edge"]):
            best = {"side": side, "price": round(avg, 4), "contracts": contracts, "fee": fee,
                    "p_win": round(p_win, 4), "edge": round(edge, 4)}
    return best


class Experiment:
    def __init__(self, store, min_edge=MIN_EDGE, stake=STAKE):
        self.store, self.min_edge, self.stake = store, min_edge, stake
        self.done = set()           # tickers already decided this run (one entry per market)

    def observe(self, ticker, series, now, mins_left, prob_yes, asks):
        """Call at a reading inside the betting window. One shadow entry per market. Never raises."""
        try:
            if ticker in self.done:
                return None
            fill = decide(prob_yes, asks, self.min_edge, self.stake)
            if fill is None:
                return None
            ensure_table(self.store)
            self.store.x("""INSERT OR IGNORE INTO experiment_bets
                (ticker, ts, series, side, price, contracts, stake, fee, p_win, edge, mins_left, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')""",
                         (ticker, int(now), series, fill["side"], fill["price"], fill["contracts"],
                          round(fill["contracts"] * fill["price"], 2), fill["fee"], fill["p_win"], fill["edge"],
                          round(float(mins_left), 2)))
            self.done.add(ticker)
            return fill
        except Exception:
            return None


def settle(store, now=None):
    """Close shadow entries whose market has settled (outcome from graded_markets). Returns how many closed."""
    ensure_table(store)
    now = int(time.time() if now is None else now)
    rows = store.q("""SELECT e.ticker, e.side, e.price, e.contracts, e.stake, e.fee, g.outcome
                      FROM experiment_bets e JOIN graded_markets g ON g.ticker = e.ticker
                      WHERE e.status = 'open' AND g.outcome IN (0, 1)""")
    for r in rows:
        won = (int(r["outcome"]) == 1) == (r["side"] == "yes")
        pnl = (r["contracts"] * (1 - r["price"]) - r["fee"]) if won else -(r["stake"] + r["fee"])
        store.x("UPDATE experiment_bets SET status='settled', outcome=?, pnl=?, settled_ts=? WHERE ticker=?",
                (int(r["outcome"]), round(pnl, 2), now, r["ticker"]))
    return len(rows)


def report(store, now=None):
    """For the app. Dollars per $100 staked, with how far the average is from zero. Safe on an empty table."""
    settle(store, now)
    rows = store.q("SELECT * FROM experiment_bets ORDER BY ts")
    done = [r for r in rows if r["status"] == "settled"]
    out = {"entries": len(rows), "settled": len(done), "open": len(rows) - len(done), "min_edge": MIN_EDGE,
           "stake": STAKE, "since": rows[0]["ts"] if rows else None}
    if not done:
        return out
    staked = sum(r["stake"] + r["fee"] for r in done)
    pnl = sum(r["pnl"] for r in done)
    rets = [r["pnl"] / (r["stake"] + r["fee"]) for r in done]
    mean = sum(rets) / len(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    out.update({
        "wins": sum(1 for r in done if r["pnl"] > 0), "pnl": round(pnl, 2), "staked": round(staked, 2),
        "per100": round(100 * pnl / staked, 2) if staked else None,
        "z": round(mean / (sd / math.sqrt(len(rets))), 2) if sd else None,
        "by_coin": {s[2:5]: {"n": n, "pnl": round(p, 2)} for s, (n, p) in _by(done, "series").items()},
    })
    return out


def _by(rows, key):
    agg = {}
    for r in rows:
        n, p = agg.get(r[key], (0, 0.0))
        agg[r[key]] = (n + 1, p + r["pnl"])
    return agg
