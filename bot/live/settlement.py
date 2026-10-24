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
    """Extract strike, deadline and averaging window from a Kalshi market."""
    rules = " ".join(str(mkt.get(k, "")) for k in ("rules_primary", "rules_secondary"))
    avg_s, source = None, "assumed"
    m = re.search(r"average[^.]{0,80}?(\d+)\s*(second|minute)", rules, re.I) or \
        re.search(r"(\d+)\s*(second|minute)s?[^.]{0,40}?average", rules, re.I)
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
    return {"strike": strike, "close_ts": close_ts, "avg_seconds": avg_s,
            "avg_source": source, "benchmark": benchmark}


def prob_above(spot, strike, vol_per_min, seconds_left, avg_seconds=60,
               realized_avg=None, tail_shrink=0.9, proxy_bps=None):
    """P(settlement value > strike), where settlement = average price over the
    final `avg_seconds`. Works in log space under a driftless random walk.

    realized_avg: average price already observed inside the window, if we're
    already in it (needed for an exact answer in the last minute).
    """
    try:
        numeric_inputs = (spot, strike, vol_per_min, seconds_left, avg_seconds, tail_shrink)
        if any(not math.isfinite(float(value)) for value in numeric_inputs):
            return None
        spot, strike, vol_per_min = float(spot), float(strike), float(vol_per_min)
        seconds_left, avg_seconds = float(seconds_left), float(avg_seconds)
        tail_shrink = float(tail_shrink)
        if realized_avg is not None:
            realized_avg = float(realized_avg)
            if not math.isfinite(realized_avg) or realized_avg <= 0:
                return None
    except (TypeError, ValueError, OverflowError):
        return None
    if spot <= 0 or strike <= 0 or vol_per_min <= 0 or avg_seconds <= 0 or tail_shrink < 0:
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
        return 1.0 if mean > k else 0.0
    z = (mean - k) / math.sqrt(var)
    return _norm_cdf(tail_shrink * z)
