"""Two safety-first systems.

1. Arbitrage scanner — the only truly "safe bet" there is. In a game with no
   possible tie, exactly one of the two team markets pays $1. If buying YES on
   both teams costs less than $1 after fees, the profit is locked in no matter
   who wins.

2. Circuit breakers — the bot benches itself instead of digging a hole:
   - drawdown: last 7 days lost more than MAX_DRAWDOWN of bankroll
   - negative CLV: recent bets consistently worse than the closing line
   - per-league: a league with a proven negative edge stops being bet
   Research and learning keep running while betting is paused.
"""
import os
import time

from .risk import asks_from_book, taker_fee

# leagues where a game can end tied (both "team wins" markets lose): no arb
TIE_POSSIBLE = {"KXNFLGAME"}


def arb_plan(series, book_a, book_b, fee_rate, max_dollars, min_profit_per=0.01):
    """Return a two-leg plan if YES(A)+YES(B) < $1 after fees, else None."""
    if series in TIE_POSSIBLE:
        return None
    a = asks_from_book(book_a)["yes"]
    b = asks_from_book(book_b)["yes"]
    if not a or not b:
        return None
    pa, qa = a[0]
    pb, qb = b[0]
    contracts = int(min(qa, qb, max_dollars // (pa + pb)))
    if contracts < 1:
        return None
    fees = taker_fee(contracts, pa, fee_rate) + taker_fee(contracts, pb, fee_rate)
    profit = contracts * (1 - pa - pb) - fees
    if profit / contracts < min_profit_per:
        return None
    return {"contracts": contracts, "price_a": pa, "price_b": pb,
            "fees": round(fees, 2), "locked_profit": round(profit, 2),
            "stake": round(contracts * (pa + pb), 2)}


def drawdown_tripped(store, bankroll, mode=None):
    pct = float(os.environ.get("MAX_DRAWDOWN", 10))
    limit = (pct / 100 if pct > 1 else pct) * bankroll
    since = int(time.time()) - 7 * 86400
    r = store.q("""SELECT COALESCE(SUM(pnl),0) p FROM bets
                   WHERE status='graded' AND graded_ts>=? AND (? IS NULL OR mode=?)""",
                (since, mode, mode))
    return r[0]["p"] < -limit


RECOVERY_DAYS = int(os.environ.get("RECOVERY_DAYS", 21))


def _since():
    # Only recent results count. Without this, a pause or bench would be
    # PERMANENT: no new bets means the bad record never rolls off.
    return int(time.time()) - RECOVERY_DAYS * 86400


def clv_tripped(store, n=50, floor=-0.01, mode=None):
    rows = store.q("""SELECT clv FROM bets WHERE status='graded' AND clv IS NOT NULL
                      AND series NOT LIKE '%15M' AND graded_ts >= ?
                      AND (? IS NULL OR mode=?) ORDER BY graded_ts DESC LIMIT ?""",
                   (_since(), mode, mode, n))
    if len(rows) < n:
        return False
    return sum(r["clv"] for r in rows) / n < floor


def league_benched(store, series, n=40):
    rows = store.q("""SELECT clv, pnl FROM bets WHERE status='graded' AND series=?
                      AND graded_ts >= ? ORDER BY graded_ts DESC LIMIT ?""", (series, _since(), n))
    if len(rows) < n:
        return False
    if series.endswith("15M"):
        # closing line is thin on 15-minute windows; judge them on P&L
        return sum(r["pnl"] or 0 for r in rows) < 0
    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    return bool(clvs) and sum(clvs) / len(clvs) < 0


def status(store, bankroll, daily_loss_limit=None, mode=None):
    """(betting_allowed, reason)."""
    from .risk import day_start_ts
    limit = daily_loss_limit if daily_loss_limit is not None else \
        float(os.environ.get("DAILY_LOSS_LIMIT", 30))
    mode = mode or os.environ.get("BOT_MODE", "paper")
    lost = store.realized_today(day_start_ts(), mode=mode)
    if lost <= -limit:
        return False, f"daily loss limit hit (${-lost:.2f} lost today; resets at midnight in {os.environ.get('BOT_TIMEZONE', 'America/Chicago')})"
    if drawdown_tripped(store, bankroll, mode=mode):
        return False, "drawdown limit hit (7-day losses)"
    if clv_tripped(store, mode=mode):
        return False, "last 50 bets lost to the closing line"
    return True, "ok"


def ladder_arbs(markets, fee_rate, min_profit_per=0.01):
    """Cross-market arbitrage on price ladders.

    markets: [{"ticker", "strike", "close_ts", "rules_key", "yes_bid", "yes_ask"}]
    for "price ABOVE strike" contracts. Two contracts settling at the same
    moment on the same rules must be consistent: "above the lower strike" can
    never be less likely than "above the higher one". Buying YES(low) + NO(high)
    pays at least $1 whatever happens (and $2 if price ends between them), so if
    that costs less than $1 after fees, the profit is locked in.

    Only pairs with identical settlement rules (same index, time and averaging)
    count — otherwise it isn't a true arbitrage and is skipped.
    """
    groups = {}
    for m in markets:
        if m.get("strike") and m.get("close_ts") and m.get("yes_ask") and m.get("yes_bid") is not None:
            groups.setdefault((m["close_ts"], m["rules_key"]), []).append(m)
    found = []
    for (_, _), ms in groups.items():
        ms = sorted(ms, key=lambda m: m["strike"])
        for i, low in enumerate(ms):
            for high in ms[i + 1:]:
                if high["strike"] <= low["strike"]:
                    continue
                yes_low = low["yes_ask"]
                no_high = 1 - high["yes_bid"]
                cost = yes_low + no_high
                fees = (taker_fee(100, yes_low, fee_rate) + taker_fee(100, no_high, fee_rate)) / 100
                profit = 1 - cost - fees
                if profit >= min_profit_per:
                    found.append({"low": low["ticker"], "high": high["ticker"],
                                  "yes_low": yes_low, "no_high": round(no_high, 4),
                                  "profit_per": round(profit, 4)})
    return sorted(found, key=lambda x: -x["profit_per"])
