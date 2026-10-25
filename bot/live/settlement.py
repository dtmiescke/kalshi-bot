"""Model the exact contract, not just "the BTC price".

Kalshi crypto contracts settle on a CF Benchmarks index, often AVERAGED over
a short window before the deadline. An average moves less than a single
price, which changes the true probability — especially in the final minute.

The averaging window is read from the market's own rules text when possible;
otherwise SETTLE_AVG_SECONDS (default 60) is used and flagged as an assumption.
Check a few contracts' rules in the Kalshi app and set it correctly.
"""
import math
import os
import re
from datetime import datetime, timezone

from ..market_rules import summarize_rules


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _positive_env_int(name, default):
    try:
        value = int(os.environ.get(name, default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _nonnegative_env_float(name, default):
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) and value >= 0 else default
    except (TypeError, ValueError):
        return default


def contract_terms(mkt):
    """Extract strike, deadline, averaging window and compact rules audit."""
    rules = " ".join(str(mkt.get(k, "")) for k in ("rules_primary", "rules_secondary"))
    avg_s, source = None, "assumed"
    m = re.search(r"average[^.]{0,80}?(\d+)\s*(second|minute)", rules, re.I)
    if not m:
        m = re.search(r"(\d+)\s*(second|minute)s?[^.]{0,40}?average", rules, re.I)
    if m:
        avg_s = int(m.group(1)) * (60 if m.group(2).lower().startswith("minute") else 1)
        source = "rules"
    if avg_s is None:
        avg_s = _positive_env_int("SETTLE_AVG_SECONDS", 60)
    benchmark = "CF Benchmarks" if "cf benchmarks" in rules.lower() else "unstated"
    strike = None
    for k in ("floor_strike", "cap_strike"):
        if mkt.get(k) not in (None, ""):
            try:
                candidate = float(mkt[k])
                if math.isfinite(candidate) and candidate > 0:
                    strike = candidate
            except (TypeError, ValueError):
                pass
            if strike is not None:
                break
    close = mkt.get("close_time")
    close_ts = None
    if close:
        try:
            parsed = datetime.fromisoformat(close.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            close_ts = parsed.timestamp()
        except ValueError:
            pass
    rules_audit = summarize_rules(mkt)
    # Do not copy multi-kilobyte raw rule text into every in-memory live-market
    # record. The shared counsel path can still request full bounded terms directly.
    rules_audit.pop("terms", None)
    return {"strike": strike, "close_ts": close_ts, "avg_seconds": avg_s,
            "avg_source": source, "benchmark": benchmark,
            "rules_audit": rules_audit}


def prob_above(spot, strike, vol_per_min, seconds_left, avg_seconds=60,
               realized_avg=None, tail_shrink=0.9, proxy_bps=None, tail_df=None, vol_scale=1.0):
    """P(settlement value > strike), where settlement = average price over the
    final `avg_seconds`. Works in log space under a driftless random walk.

    realized_avg: average price already observed inside the window, if we're
    already in it (needed for an exact answer in the last minute).

    Shape of the move: with tail_df set (a fitted tail model, vol.tail_model),
    a unit-variance Student-t with that many degrees of freedom and the
    volatility multiplied by vol_scale; tail_shrink is then not used. Without
    it, a normal with z multiplied by tail_shrink.
    """
    try:
        shrink, scale = float(tail_shrink), float(vol_scale)
        df = None if tail_df is None else float(tail_df)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(shrink) or shrink < 0 or not math.isfinite(scale) or scale <= 0:
        return None
    if df is not None and (not math.isfinite(df) or df <= 2):
        return None
    try:
        vol = float(vol_per_min) * scale
    except (TypeError, ValueError):
        return None
    z = z_above(spot, strike, vol, seconds_left, avg_seconds, realized_avg, proxy_bps)
    if z is None:
        return None
    if math.isinf(z):
        return 1.0 if z > 0 else 0.0
    if df is not None:
        from scipy.special import stdtr
        return float(stdtr(df, z * math.sqrt(df / (df - 2))))
    return _norm_cdf(shrink * z)


def z_above(spot, strike, vol_per_min, seconds_left, avg_seconds=60,
            realized_avg=None, proxy_bps=None):
    """The un-shrunk z-score prob_above prices with: (mean - log strike) / sd of
    the settlement average, proxy variance included. +/-inf when there is no
    variance left; None on invalid input. Logged so tail fitting uses exactly
    the calculation the bot bets with."""
    try:
        numeric_inputs = (spot, strike, vol_per_min, seconds_left, avg_seconds)
        if any(not math.isfinite(float(value)) for value in numeric_inputs):
            return None
        spot, strike, vol_per_min = float(spot), float(strike), float(vol_per_min)
        seconds_left, avg_seconds = float(seconds_left), float(avg_seconds)
        if realized_avg is not None:
            realized_avg = float(realized_avg)
            if not math.isfinite(realized_avg) or realized_avg <= 0:
                return None
    except (TypeError, ValueError, OverflowError):
        return None
    if spot <= 0 or strike <= 0 or vol_per_min <= 0 or avg_seconds <= 0:
        return None
    s2 = (vol_per_min ** 2) / 60.0          # variance per second
    x, k = math.log(spot), math.log(strike)
    tau, A = max(seconds_left, 0.0), max(avg_seconds, 1)
    if tau >= A:
        mean = x
        var = s2 * ((tau - A) + A / 3.0)
    else:
        done = A - tau
        r = math.log(realized_avg) if realized_avg else x
        mean = (done * r + tau * x) / A
        var = (tau / A) ** 2 * s2 * tau / 3.0
    # Our price (Coinbase/Kraken) is not Kalshi's settlement index (CF Benchmarks).
    # That gap is extra uncertainty that matters most right at the strike, so
    # it's added as variance: it stops the model calling a coin-flip a sure thing.
    if proxy_bps is None:
        proxy_bps = _nonnegative_env_float("SETTLE_PROXY_BPS", 3)
    try:
        proxy_bps = float(proxy_bps)
    except (TypeError, ValueError):
        proxy_bps = 3.0
    if not math.isfinite(proxy_bps) or proxy_bps < 0:
        proxy_bps = 3.0
    var += (proxy_bps / 1e4) ** 2
    if var <= 1e-14:
        return math.inf if mean > k else -math.inf
    return (mean - k) / math.sqrt(var)
