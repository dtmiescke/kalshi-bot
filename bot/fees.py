"""Kalshi trading fees: the ONE fee implementation, so every edge and paper
profit is measured AFTER fees. `risk.taker_fee` delegates here.

Formula (Kalshi General Trading Fees Table): fee = rate * contracts * P * (1 - P),
P = contract price in dollars, rounded UP to the cent. Kalshi itself rounds to
$0.0001 per order for direct accounts (docs: getting_started/fee_rounding), so
the cent ceiling slightly overstates fees: conservative, never understated.

Which rate applies comes from each series' Kalshi `fee_type` and `fee_multiplier`
(GET /series/{ticker}; docs: api-reference/market/get-series):
- quadratic                        taker fees only; resting (maker) fills are free
- quadratic_with_maker_fees        taker fees + maker fees (maker multiplier 0.25)
- quadratic_with_combo_maker_fees  same, maker multiplier 0.5
- flat                             Specific Trading Fees Table (not modelled ->
                                   conservative configured rates)
fee_multiplier scales every fee in the series. A series we know nothing about is
charged the configured TAKER_FEE_RATE / MAKER_FEE_RATE with multiplier 1.

The general taker rate (0.07) could not be re-read from Kalshi's fee schedule PDF
on 2026-10-25 (rate-limited); it stays configurable via TAKER_FEE_RATE.
"""
import logging
import math
import time

log = logging.getLogger("bot")

DEFAULT_TAKER_RATE = 0.07
DEFAULT_MAKER_RATE = 0.0175            # 0.25 x the taker rate
MAKER_SHARE = {"quadratic_with_maker_fees": 0.25, "quadratic_with_combo_maker_fees": 0.5}
FEE_TYPES = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees", "flat")

# Read from Kalshi's public GET /series/{ticker} on 2026-10-25. Refreshed at
# runtime by refresh(); this table only covers the gap before the first refresh.
SERIES_FEES = {
    "KXBTC15M": ("quadratic", 1.0), "KXETH15M": ("quadratic", 1.0),
    "KXSOL15M": ("quadratic", 1.0), "KXBTCD": ("quadratic", 1.0), "KXETHD": ("quadratic", 1.0),
    "KXNFLGAME": ("quadratic_with_maker_fees", 1.0),
    "KXNCAAFGAME": ("quadratic_with_maker_fees", 1.0),
    "KXNBAGAME": ("quadratic_with_maker_fees", 1.0),
    "KXMLBGAME": ("quadratic_with_maker_fees", 0.5),
    "KXNHLGAME": ("quadratic_with_maker_fees", 1.0),
}
_live = {}                              # fresher values from refresh()/load()


def _ceil_cent(x):
    # Round UP to the next cent; tiny epsilon avoids float noise like 0.0700000001
    return math.ceil(round(x * 100, 9)) / 100.0


def series_of(ticker_or_series):
    """KXNBAGAME-26OCT24BOSNYK-BOS -> KXNBAGAME."""
    return str(ticker_or_series or "").split("-", 1)[0].upper()


def _clean(fee_type, multiplier):
    """(fee_type, multiplier) if both are understood, else None."""
    try:
        m = float(multiplier)
    except (TypeError, ValueError):
        return None
    if fee_type not in FEE_TYPES or not math.isfinite(m) or not 0 < m <= 10:
        return None
    return fee_type, m


def lookup(series):
    s = series_of(series)
    return _live.get(s) or SERIES_FEES.get(s)


def rate(series=None, settings=None, maker=False):
    """Fee rate for a fill in `series`: taker by default, maker for resting fills."""
    taker = float(getattr(settings, "taker_fee_rate", DEFAULT_TAKER_RATE))
    maker_rate = float(getattr(settings, "maker_fee_rate", DEFAULT_MAKER_RATE))
    info = lookup(series)
    if info is None or info[0] == "flat":
        return maker_rate if maker else taker            # unknown: conservative
    fee_type, mult = info
    if not maker:
        return taker * mult
    if fee_type == "quadratic":
        return 0.0                                       # no maker fees in this series
    if fee_type == "quadratic_with_maker_fees":
        return maker_rate * mult
    return taker * MAKER_SHARE[fee_type] * mult


def fee_at_rate(contracts, price, rate_):
    """Total fee in dollars for `contracts` at `price` (0-1) and a given rate."""
    if contracts <= 0 or rate_ <= 0:
        return 0.0
    return _ceil_cent(rate_ * contracts * price * (1.0 - price))


def fee(price, contracts, series=None, maker=False, settings=None):
    """Total fee in dollars for buying `contracts` at `price` (0-1 dollars)."""
    if contracts <= 0:
        return 0.0
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be between 0 and 1 dollars, got {price}")
    return fee_at_rate(contracts, price, rate(series, settings, maker))


def fee_per_contract(price, contracts=1, series=None, maker=False, settings=None):
    return fee(price, contracts, series, maker, settings) / max(contracts, 1)


def edge_after_fees(prob, price, contracts=1, series=None, maker=False, settings=None):
    """Expected profit per contract after fees, in dollars.

    prob  = the bot's probability the side it buys wins (0-1)
    price = what it pays for that side (0-1)
    """
    gross = prob - price
    # Round to kill float noise (0.52 - 0.50 - 0.02 must be exactly 0, not 1.7e-17)
    return round(gross - fee_per_contract(price, contracts, series, maker, settings), 9)


def settle_pnl(price, contracts, won, series=None, maker=False, settings=None):
    """Realized profit for a settled position, fees included."""
    cost = price * contracts + fee(price, contracts, series, maker, settings)
    payout = contracts * 1.0 if won else 0.0
    return round(payout - cost, 2)


# --- keeping the series table current -------------------------------------------------
def load(store):
    """Use the last refreshed fee table saved in the database."""
    for s, v in (store.get_kv("series_fees") or {}).items():
        info = _clean(v.get("fee_type"), v.get("multiplier"))
        if info:
            _live[s] = info


def refresh(store, client, series, every_hours=24, now=None):
    """Re-read fee_type/fee_multiplier from Kalshi for these series, at most daily.
    A failed or unrecognised response keeps the previous value."""
    now = time.time() if now is None else now
    saved = store.get_kv("series_fees") or {}
    changed = {}
    for s in {series_of(x) for x in series if x}:
        if s in saved and now - float(saved[s].get("ts", 0)) < every_hours * 3600:
            continue
        try:
            info = client._req("GET", f"/series/{s}").get("series", {})
        except Exception as e:
            log.info("fee refresh %s: %s", s, e)
            continue
        clean = _clean(info.get("fee_type"), info.get("fee_multiplier"))
        if not clean:
            log.warning("fee refresh %s: unrecognised fee info %r", s,
                        {k: info.get(k) for k in ("fee_type", "fee_multiplier")})
            continue
        old = saved.get(s)
        if old and (old.get("fee_type"), old.get("multiplier")) != clean:
            changed[s] = {"from": [old.get("fee_type"), old.get("multiplier")], "to": list(clean)}
        saved[s] = {"fee_type": clean[0], "multiplier": clean[1], "ts": int(now)}
        _live[s] = clean
    store.set_kv("series_fees", saved)
    return changed
