"""Fees, edge, bet sizing and hard risk caps."""
import math
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import brain, fees
from .adaptive_aggression import AggressionPolicy, choose, safe_settled_paper_results
from .contextual_calibration import contextual_policy



def taker_fee(contracts, price, rate):
    """Kalshi fee in dollars at `rate`, rounded up to the cent (see fees.py)."""
    return fees.fee_at_rate(contracts, price, rate)


def asks_from_book(book):
    """Kalshi books list BIDS only. A YES ask is the mirror of a NO bid:
    buying YES at 0.62 = someone bidding NO at 0.38."""
    yes_asks = sorted(((round(1 - p, 4), q) for p, q in book.get("no", [])),
                      key=lambda x: x[0])
    no_asks = sorted(((round(1 - p, 4), q) for p, q in book.get("yes", [])),
                     key=lambda x: x[0])
    return {"yes": yes_asks, "no": no_asks}


def fill_plan(asks, max_dollars, limit_price):
    """Walk the ask ladder up to limit_price. Returns (contracts, avg_price)."""
    contracts, cost = 0, 0.0
    for p, q in asks:
        if p > limit_price:
            break
        room = int((max_dollars - cost) // p) if p > 0 else 0
        take = min(int(q), room)
        if take <= 0:
            break
        contracts += take
        cost += take * p
    return contracts, (cost / contracts if contracts else None)


def fill_plan_contracts(asks, max_contracts, limit_price):
    """Recalculate average cost for an exact target size using only available depth."""
    contracts, cost = 0, 0.0
    remaining = max(0, int(max_contracts))
    for p, q in asks:
        if p > limit_price or remaining <= 0:
            break
        take = min(int(q), remaining)
        if take <= 0:
            continue
        contracts += take
        cost += take * p
        remaining -= take
    return contracts, (cost / contracts if contracts else None)


def day_start_ts(now_ts=None, tz_name=None):
    """UTC timestamp for midnight in the configured reporting/risk timezone.

    Using one timezone for daily loss and daily spend limits avoids a reset
    that disagrees with the dashboard. The UTC fallback is retained if a
    configured timezone name is invalid.
    """
    tz_name = tz_name or os.environ.get("BOT_TIMEZONE", "America/Chicago")
    try:
        local_tz = ZoneInfo(tz_name)
    except Exception:
        local_tz = timezone.utc
    now_utc = datetime.fromtimestamp(time.time() if now_ts is None else now_ts, timezone.utc)
    local_now = now_utc.astimezone(local_tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(local_midnight.astimezone(timezone.utc).timestamp())


def window_key(event_ticker):
    """KXBTC15M-26OCT191830 and KXETH15M-26OCT191830 settle together: same key."""
    return event_ticker.split("-", 1)[1] if "-" in (event_ticker or "") else event_ticker


def staked_on_window(store, event_ticker, mode=None):
    key = window_key(event_ticker)
    r = store.q("""SELECT COALESCE(SUM(stake + fee),0) s FROM bets WHERE status='open'
                   AND series LIKE '%15M' AND event_ticker LIKE ?
                   AND (? IS NULL OR mode=?)""", (f"%-{key}", mode, mode))
    return r[0]["s"]


def earned_size(store, mkt, full_at=200, floor=0.25, mode=None):
    """Size up only on profitable graded history from this account mode/domain."""
    dom = brain.domain_of(mkt.get("series") or mkt.get("ticker"))
    mode = mode or os.environ.get("BOT_MODE", "paper")
    rows = store.q("SELECT series, pnl FROM bets WHERE status='graded' AND mode=?", (mode,))
    mine = [r for r in rows if brain.domain_of(r["series"]) == dom]
    if not mine or sum(r["pnl"] or 0 for r in mine) <= 0:
        return floor
    return max(floor, min(1.0, (len(mine) / full_at) ** 0.5))


def bid_for(book, side):
    levels = book.get(side, [])
    return levels[0][0] if levels else None


def crypto_model_prob(readings):
    """The raw exact-contract YES probability, before blending, if present."""
    return next((r.prob for r in readings or () if r.name == "crypto_model"), None)


def decide(s, store, mkt, model_prob, confidence, book, min_edge=None, max_per_bet=None,
           maker=False, raw_model_prob=None):
    """Pick the best side (YES or NO), check edge after fees and every cap.

    maker=True prices a resting limit order one tick above the best bid (cheaper,
    lower fee, but may not fill) instead of paying the ask now.
    raw_model_prob: the unblended crypto_model YES probability; crypto long shots
    (below s.crypto_longshot_price) need it to show the edge on its own.
    Returns (plan, "ok") or (None, reason).
    """
    min_edge = s.min_edge if min_edge is None else min_edge
    max_per_bet = s.max_per_bet if max_per_bet is None else max_per_bet
    min_confidence = s.min_confidence
    effective_kelly_fraction = s.kelly_fraction
    if s.mode == "paper":
        aggression_level = getattr(s, "aggression_level", "off")
        history = (safe_settled_paper_results(store)
                   if aggression_level == "adaptive" else ())
        policy = choose(AggressionPolicy(
            level=aggression_level,
            min_edge=s.min_edge, min_confidence=s.min_confidence,
            kelly_fraction=s.kelly_fraction),
            settled_paper_results=history)
        if policy and aggression_level == "adaptive":
            policy = contextual_policy(policy, model_prob=model_prob,
                                       confidence=confidence, book=book)
        if policy:
            if min_edge == s.min_edge:
                min_edge = max(s.min_edge, policy["min_edge"])
            min_confidence = policy["min_confidence"]
            effective_kelly_fraction = min(s.kelly_fraction, policy["kelly_fraction"])
    if confidence < min_confidence:
        return None, f"confidence {confidence:.0%} below {min_confidence:.0%}"
    if store.has_open_event_bet(mkt["event_ticker"], mode=s.mode):
        # YES on Boston and NO on New York are the same bet; never double up
        return None, "already holding this game"
    is_crypto = str(mkt.get("series", "")).endswith("15M")
    crypto_domain = brain.domain_of(mkt.get("series") or mkt.get("ticker")) == "crypto"
    if is_crypto and store.q("SELECT 1 FROM bets WHERE event_ticker=? AND mode=? LIMIT 1",
                             (mkt["event_ticker"], s.mode)):
        # one entry per 15-minute window: after an early exit, re-buying the same
        # window just pays the spread and fees twice
        return None, "already traded this window"

    asks = asks_from_book(book)
    options = []
    longshot_cap = getattr(s, "crypto_longshot_price", 0.0) or 0.0
    crypto_domain = brain.domain_of(mkt.get("series") or mkt.get("ticker")) == "crypto"
    longshot_blocked = None
    for side, p_win in (("yes", model_prob), ("no", 1 - model_prob)):
        if not asks[side]:
            continue
        best_ask = asks[side][0][0]
        series = mkt.get("series") or mkt.get("ticker")
        price, rate, style = best_ask, fees.rate(series, s), "taker"
        bid = bid_for(book, side)
        if maker and bid is not None and round(bid + 0.01, 2) < best_ask:
            price, rate, style = round(bid + 0.01, 2), fees.rate(series, s, maker=True), "maker"
        if not (s.min_price <= price <= s.max_price):
            continue
        fee_per = taker_fee(100, price, rate) / 100
        if crypto_domain and price < longshot_cap:
            # Long shots are where a too-fat tail or a market-anchored blend
            # invents edge. The raw contract model must agree on its own.
            raw = None if raw_model_prob is None else (
                raw_model_prob if side == "yes" else 1 - raw_model_prob)
            if raw is None or raw - price - fee_per < min_edge:
                longshot_blocked = (side, price, raw)
                continue
        options.append((p_win - price - fee_per, side, p_win, price, best_ask, rate, style))
    if longshot_blocked and (not options or max(options)[0] < min_edge):
        # Name the guard, not the leftover side's shortfall, so audits show why.
        side, price, raw = longshot_blocked
        support = "no raw model" if raw is None else f"raw model {raw:.1%}"
        return None, (f"crypto long shot: {side.upper()} @ ${price:.2f} below "
                      f"${longshot_cap:.2f} without model support ({support})")
    if not options:
        return None, "no tradable price"
    edge, side, p_win, price, best_ask, rate, style = max(options)
    if edge < min_edge:
        return None, f"best edge {edge:+.1%} below {min_edge:.0%}"

    earned = earned_size(store, mkt, mode=s.mode)
    if s.fixed_bet > 0:
        # A fixed target is still scaled by the earned-size safety ramp.
        want = max(min(s.fixed_bet, 1.0), s.fixed_bet * earned)
    else:   # fractional Kelly on the FEE-INCLUSIVE cost, scaled by confidence
        cost = price + taker_fee(100, price, rate) / 100
        kelly = max(0.0, (p_win - cost) / (1 - cost))
        want = s.bankroll * effective_kelly_fraction * kelly * confidence * earned
    crypto_min_bet = max(0.0, float(getattr(s, "crypto_min_bet", 15.0))) if crypto_domain else 0.0
    if crypto_domain:
        # Never place a smaller crypto trade merely because Kelly sizing is small.
        # If the full minimum cannot pass every cap and fee check, skip the trade.
        want = max(want, crypto_min_bet)

    acct = store.account(s.bankroll, mode=s.mode)
    if acct["open_positions"] >= s.max_open_positions:
        return None, f"{acct['open_positions']} positions open (max {s.max_open_positions})"
    left_day = s.max_per_day - store.staked_since(day_start_ts(), mode=s.mode)
    left_game = s.max_per_game - store.staked_on_event(mkt["event_ticker"], mode=s.mode)
    left_open = s.max_open_cost - acct["open_cost"]
    # leave room for the fee too: a bet can never spend cash the account doesn't have
    left_cash = acct["cash"] - 0.05
    caps = [want, max_per_bet, left_day, left_game, left_open, left_cash]
    if str(mkt.get("series", "")).endswith("15M"):
        caps.append(s.crypto_max_per_window - staked_on_window(store, mkt["event_ticker"], mode=s.mode))
    budget = min(caps)
    if budget < 1.0:
        return None, "risk caps reached"
    min_total_cost = crypto_min_bet + taker_fee(max(1, int(crypto_min_bet / max(price, 0.01))), price, rate)
    if crypto_domain and budget + 1e-9 < min_total_cost:
        return None, f"crypto minimum ${crypto_min_bet:.2f} unavailable under risk/account caps"

    if style == "maker":
        contracts, avg = int(budget // price), price
        limit = price
    else:
        # don't chase: allow at most 1c worse than best ask, and keep edge positive
        limit = round(min(best_ask + 0.01, p_win - min_edge), 2)
        contracts, avg = fill_plan(asks[side], budget, limit)
    if contracts < 1:
        return None, "not enough size at a good price"

    # Reconcile exact depth and cent-rounded fees before returning a plan.
    # A budget is total dollars at risk, not just the contract premium.
    while contracts > 0:
        if style == "maker":
            avg = price
        else:
            contracts, avg = fill_plan_contracts(asks[side], contracts, limit)
            if contracts < 1:
                break
        fee = taker_fee(contracts, avg, rate)
        stake = contracts * avg
        if stake + fee <= budget + 1e-9 and stake + fee <= acct["cash"] - 0.05 + 1e-9:
            break
        contracts -= 1
    if contracts < 1:
        return None, "fees exceed available budget"
    fee = taker_fee(contracts, avg, rate)
    if crypto_domain and contracts * avg + 1e-9 < crypto_min_bet:
        return None, f"crypto minimum ${crypto_min_bet:.2f} cannot be filled at an eligible price"
    # Final check on the REAL order: fees round up per order, so small orders
    # and deeper fills can turn a "5% edge" into 3%. Re-test after actual costs.
    real_edge = p_win - avg - fee / contracts
    if real_edge < min_edge:
        return None, f"edge after real fees {real_edge:+.1%} below {min_edge:.0%}"
    return {
        "side": side, "price": round(avg, 4), "limit": limit, "style": style,
        "contracts": contracts, "stake": round(contracts * avg, 2), "fee": fee,
        "p_win": p_win, "edge": round(p_win - avg - fee / contracts, 4),
    }, "ok"
