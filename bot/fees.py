"""Kalshi trading fees, so every edge and paper profit is measured AFTER fees.

Kalshi's published fee shape is: fee = ceil_to_cent(rate * contracts * P * (1 - P)),
where P is the contract price in dollars. Fees are largest near 50c and tiny near
1c or 99c. Taker and maker rates differ, and some series use their own rates.

VERIFY ON BUILD DAY: confirm current rates against Kalshi's fee schedule and
put any per-series overrides in SERIES_RATES. Defaults below are placeholders
chosen to be conservative (never understate fees).
"""
import math

DEFAULT_TAKER_RATE = 0.07
DEFAULT_MAKER_RATE = 0.0175

# Per-series overrides, e.g. {"KXBTC15M": {"taker": 0.07, "maker": 0.0175}}
SERIES_RATES = {}


def _ceil_cent(x):
    # Round UP to the next cent; tiny epsilon avoids float noise like 0.0700000001
    return math.ceil(round(x * 100, 9)) / 100.0


def rate_for(series=None, maker=False):
    r = SERIES_RATES.get(series or "", {})
    if maker:
        return r.get("maker", DEFAULT_MAKER_RATE)
    return r.get("taker", DEFAULT_TAKER_RATE)


def fee(price, contracts, series=None, maker=False):
    """Total fee in dollars for buying `contracts` at `price` (0-1 dollars)."""
    if contracts <= 0:
        return 0.0
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be between 0 and 1 dollars, got {price}")
    raw = rate_for(series, maker) * contracts * price * (1.0 - price)
    return _ceil_cent(raw)


def fee_per_contract(price, contracts=1, series=None, maker=False):
    return fee(price, contracts, series, maker) / max(contracts, 1)


def edge_after_fees(prob, price, contracts=1, series=None, maker=False):
    """Expected profit per contract after fees, in dollars.

    prob  = the bot's probability the side it buys wins (0-1)
    price = what it pays for that side (0-1)
    """
    gross = prob - price
    # Round to kill float noise (0.52 - 0.50 - 0.02 must be exactly 0, not 1.7e-17)
    return round(gross - fee_per_contract(price, contracts, series, maker), 9)


def settle_pnl(price, contracts, won, series=None, maker=False):
    """Realized profit for a settled position, fees included."""
    cost = price * contracts + fee(price, contracts, series, maker)
    payout = contracts * 1.0 if won else 0.0
    return round(payout - cost, 2)
