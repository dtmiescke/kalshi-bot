"""What an experienced crypto chart trader looks at, as numbers the bot can test.

Each `ex_*` value is one reading a discretionary trader would make from the last hours of
1-minute candles: market structure, liquidity sweeps, volatility squeezes, RSI divergence,
absorption, where price sits in the hour's range, the session. They are hypotheses, not
beliefs. bot/hindsight.py turns each into a rule, scores it on every settled window, and the
playbook only lets one influence a bet after it has paid, after fees, on 50+ windows in the
market condition where it is used.

Values are unitless or in volatility units, so they mean the same for BTC and SOL. A reading
that the candles can't support is left out (the rules then abstain)."""
import math

import numpy as np

from . import features

MIN_CANDLES = 70


def _arr(candles, key):
    return np.array([float(c[key]) for c in candles])


def _sgn(x):
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


def structure(candles):
    """Market structure over the last hour of 5-minute bars: +1 = every bar makes a higher
    high and a higher low (an uptrend a trader would call 'clean'), -1 = lower lows and highs."""
    b = features.resample(candles, 5)[-13:-1]          # complete bars only
    if len(b) < 8:
        return None
    h, l = _arr(b, "high"), _arr(b, "low")
    return float((np.sign(np.diff(h)).mean() + np.sign(np.diff(l)).mean()) / 2)


def squeeze_percentile(candles):
    """Where the 20-bar Bollinger width stands among the last two hours (0 = tightest)."""
    c = _arr(candles, "close")[-140:]
    if len(c) < 60:
        return None
    widths = [c[i - 20:i].std(ddof=1) / c[i - 20:i].mean() for i in range(20, len(c) + 1)]
    cur = widths[-1]
    return float(sum(w <= cur for w in widths) / len(widths))


def sweep(candles):
    """Liquidity sweep: price pierced the prior half-hour's high or low in the last 5 minutes and
    closed back inside it, which traders read as a failed breakout. +1 = swept the low and
    reclaimed (bullish), -1 = swept the high and was rejected (bearish)."""
    h, l, c = _arr(candles, "high"), _arr(candles, "low"), _arr(candles, "close")
    prior_hi, prior_lo = h[-35:-5].max(), l[-35:-5].min()
    swept_lo = l[-5:].min() < prior_lo and c[-1] > prior_lo
    swept_hi = h[-5:].max() > prior_hi and c[-1] < prior_hi
    return 1.0 if swept_lo and not swept_hi else -1.0 if swept_hi and not swept_lo else 0.0


def rsi_divergence(candles):
    """Price makes a new low (high) while RSI does not: +1 bullish, -1 bearish divergence,
    comparing the last 10 minutes with the 10-30 minutes before."""
    c = _arr(candles, "close")[-60:]
    r = features.rsi_series(c[-40:], 14)
    if len(r) < 31:
        return None
    px_recent, px_prior = c[-10:], c[-30:-10]
    r_recent, r_prior = r[-10:], r[-30:-10]
    if px_recent.min() < px_prior.min() and r_recent.min() > r_prior.min() + 2:
        return 1.0
    if px_recent.max() > px_prior.max() and r_recent.max() < r_prior.max() - 2:
        return -1.0
    return 0.0


def absorption(candles):
    """Effort without result: heavy volume in the last 5 minutes but a small range. Signed by where
    it happened in the half-hour range (absorbed at the lows is bullish, at the highs bearish)."""
    h, l, c, v = (_arr(candles, k) for k in ("high", "low", "close", "volume"))
    base_v = v[-60:].mean()
    if base_v <= 0:
        return None
    rng5 = h[-5:].max() - l[-5:].min()
    atr5 = features.atr(h, l, c, 14)
    if not atr5 or rng5 <= 0:
        return None
    heavy, tight = v[-5:].mean() / base_v >= 1.5, rng5 <= 2.5 * atr5
    if not (heavy and tight):
        return 0.0
    lo, hi = l[-30:].min(), h[-30:].max()
    pos = (c[-1] - lo) / (hi - lo) if hi > lo else 0.5
    return 1.0 if pos <= 0.3 else -1.0 if pos >= 0.7 else 0.0


def build(candles, strike=None, secs_left=None, now_ts=None):
    """The ex_* dict for one decision moment (may be empty). candles: 1-minute, oldest first."""
    if not candles or len(candles) < MIN_CANDLES:
        return {}
    c, h, l = _arr(candles, "close"), _arr(candles, "high"), _arr(candles, "low")
    spot = c[-1]
    out = {}
    for key, fn in (("ex_structure", structure), ("ex_squeeze_pct", squeeze_percentile), ("ex_sweep", sweep),
                    ("ex_rsi_div", rsi_divergence), ("ex_absorption", absorption)):
        try:
            v = fn(candles)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            v = None
        if v is not None and math.isfinite(v):
            out[key] = round(float(v), 4)
    # where price and the target sit in the last hour's range, and how much room is left
    hi, lo = h[-60:].max(), l[-60:].min()
    if hi > lo:
        out["ex_range_pos"] = round(float((spot - lo) / (hi - lo)), 4)
        if strike:
            out["ex_strike_range_pos"] = round(float((strike - lo) / (hi - lo)), 4)
        r = np.diff(np.log(c[-61:]))
        vol = float(r.std(ddof=1)) if len(r) > 5 else 0.0
        mins = max((secs_left if secs_left else 900) / 60.0, 1e-3)
        sig = vol * math.sqrt(mins)
        if sig > 0:
            out["ex_res_sigma"] = round(float(math.log(hi / spot) / sig), 4)     # room up to the hour's high
            out["ex_sup_sigma"] = round(float(math.log(spot / lo) / sig), 4)     # room down to the hour's low
            if strike and secs_left is not None:
                out["ex_pin"] = 1.0 if secs_left <= 240 and abs(math.log(spot / strike)) / sig < 0.3 else 0.0
    if now_ts is not None:
        hr = (now_ts % 86400) / 3600
        out["ex_us_open"] = 1.0 if 13.5 <= hr < 15.5 else 0.0          # US cash open hours (EDT)
        out["ex_session"] = 0.0 if hr < 7 else 1.0 if hr < 13.5 else 2.0   # 0 Asia, 1 Europe, 2 US
    return out
