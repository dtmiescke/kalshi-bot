"""Fees, edge, bet sizing and hard risk caps."""
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import brain


def taker_fee(contracts, price, rate):
    """Kalshi-style taker fee in dollars, rounded up to the cent."""
    return math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9) / 100


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


def staked_on_window(store, event_ticker):
    key = window_key(event_ticker)
    r = store.q("""SELECT COALESCE(SUM(stake),0) s FROM bets WHERE status='open'
                   AND series LIKE '%15M' AND event_ticker LIKE ?""", (f"%-{key}",))
    return r[0]["s"]


def earned_size(store, mkt, full_at=200, floor=0.25):
    """Start small, size up only with proof. 25% of normal size with no record,
    scaling to 100% after `full_at` graded bets in this domain — and only if
    those bets are net profitable. A losing record stays at the floor."""
    dom = brain.domain_of(mkt.get("series") or mkt.get("ticker"))
    rows = store.q("SELECT series, pnl FROM bets WHERE status='graded'")
    mine = [r for r in rows if brain.domain_of(r["series"]) == dom]
    if not mine or sum(r["pnl"] or 0 for r in mine) <= 0:
        return floor
    return max(floor, min(1.0, (len(mine) / full_at) ** 0.5))


def bid_for(book, side):
    levels = book.get(side, [])
    return levels[0][0] if levels else None


def decide(s, store, mkt, model_prob, confidence, book, min_edge=None, max_per_bet=None,
           maker=False):
    """Pick the best side (YES or NO), check edge after fees and every cap.

    maker=True prices a resting limit order one tick above the best bid (cheaper,
    lower fee, but may not fill) instead of paying the ask now.
    Returns (plan, "ok") or (None, reason).
    """
    min_edge = s.min_edge if min_edge is None else min_edge
    max_per_bet = s.max_per_bet if max_per_bet is None else max_per_bet
    if confidence < s.min_confidence:
        return None, f"confidence {confidence:.0%} below {s.min_confidence:.0%}"
    if store.has_open_event_bet(mkt["event_ticker"]):
        # YES on Boston and NO on New York are the same bet; never double up
        return None, "already holding this game"
    is_crypto = str(mkt.get("series", "")).endswith("15M")
    if is_crypto and store.q("SELECT 1 FROM bets WHERE event_ticker=? LIMIT 1",
                             (mkt["event_ticker"],)):
        # one entry per 15-minute window: after an early exit, re-buying the same
        # window just pays the spread and fees twice
        return None, "already traded this window"

    asks = asks_from_book(book)
    options = []
    for side, p_win in (("yes", model_prob), ("no", 1 - model_prob)):
        p_win = brain.calibrate(p_win, store, brain.domain_of(mkt.get("series") or mkt["ticker"]))
        if not asks[side]:
            continue
        best_ask = asks[side][0][0]
        price, rate, style = best_ask, s.taker_fee_rate, "taker"
        bid = bid_for(book, side)
        if maker and bid is not None and round(bid + 0.01, 2) < best_ask:
            price, rate, style = round(bid + 0.01, 2), s.maker_fee_rate, "maker"
        if not (s.min_price <= price <= s.max_price):
            continue
        fee_per = taker_fee(100, price, rate) / 100
        options.append((p_win - price - fee_per, side, p_win, price, best_ask, rate, style))
    if not options:
        return None, "no tradable price"
    edge, side, p_win, price, best_ask, rate, style = max(options)
    if edge < min_edge:
        return None, f"best edge {edge:+.1%} below {min_edge:.0%}"

    earned = earned_size(store, mkt)
    if s.fixed_bet > 0:
        # A fixed target is still scaled by the earned-size safety ramp.
        want = s.fixed_bet * earned
    else:   # fractional Kelly on the FEE-INCLUSIVE cost, scaled by confidence
        cost = price + taker_fee(100, price, rate) / 100
        kelly = max(0.0, (p_win - cost) / (1 - cost))
        want = s.bankroll * s.kelly_fraction * kelly * confidence * earned

    acct = store.account(s.bankroll)
    if acct["open_positions"] >= s.max_open_positions:
        return None, f"{acct['open_positions']} positions open (max {s.max_open_positions})"
    left_day = s.max_per_day - store.staked_since(day_start_ts())
    left_game = s.max_per_game - store.staked_on_event(mkt["event_ticker"])
    left_open = s.max_open_cost - acct["open_cost"]
    # leave room for the fee too: a bet can never spend cash the account doesn't have
    left_cash = acct["cash"] - 0.05
    caps = [want, max_per_bet, left_day, left_game, left_open, left_cash]
    if str(mkt.get("series", "")).endswith("15M"):
        caps.append(s.crypto_max_per_window - staked_on_window(store, mkt["event_ticker"]))
    budget = min(caps)
    if budget < 1.0:
        return None, "risk caps reached"

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
