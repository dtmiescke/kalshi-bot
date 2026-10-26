"""Every trading tool, computed locally for free from 1-minute candles.

Indicators: EMA/MACD, RSI, Stochastic RSI, Bollinger %B and width, ATR,
ADX, VWAP distance, OBV slope, rate of change, volume spikes, day high/low,
round-number levels, and multi-timeframe versions (1m, 5m, 15m, 60m).

Everything is SCALED so it means the same thing at $30k or $100k BTC
(distances in volatility units, not dollars). The trained model decides which
of these actually help; nothing here is a buy/sell rule on its own.

The same function builds features for training (history) and live trading,
so the model never sees a feature computed differently than it was trained on.
"""
import math
import time

import numpy as np

FEATURE_VERSION = 3


def _arr(candles, key):
    return np.array([c[key] for c in candles], dtype=float)


def resample(candles, minutes):
    """1-minute candles -> N-minute candles (aligned to the clock)."""
    out, cur = [], None
    for c in candles:
        bucket = c["ts"] - c["ts"] % (minutes * 60)
        if cur is None or cur["ts"] != bucket:
            if cur:
                out.append(cur)
            cur = {"ts": bucket, "open": c["open"], "high": c["high"], "low": c["low"],
                   "close": c["close"], "volume": c["volume"]}
        else:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] += c["volume"]
    if cur:
        out.append(cur)
    return out


def ema(x, n):
    if len(x) == 0:
        return x
    a = 2 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def rsi(close, n=14):
    if len(close) < n + 1:
        return None
    d = np.diff(close)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = ema(up, 2 * n - 1)[-1], ema(dn, 2 * n - 1)[-1]   # Wilder smoothing
    if ad == 0:
        return 100.0 if au > 0 else 50.0
    return 100 - 100 / (1 + au / ad)


def rsi_series(close, n=14):
    return np.array([rsi(close[: i + 1], n) or 50.0 for i in range(len(close))])


def stoch_rsi(close, n=14):
    if len(close) < 2 * n + 2:
        return None
    r = rsi_series(close[-(3 * n):], n)[-n:]
    lo, hi = r.min(), r.max()
    return float((r[-1] - lo) / (hi - lo)) if hi > lo else 0.5


def atr(high, low, close, n=14):
    if len(close) < n + 1:
        return None
    tr = np.maximum(high[1:] - low[1:], np.maximum(abs(high[1:] - close[:-1]),
                                                   abs(low[1:] - close[:-1])))
    return float(ema(tr, 2 * n - 1)[-1])


def adx(high, low, close, n=14):
    """Trend STRENGTH (not direction): <20 weak, >25 trending."""
    if len(close) < 2 * n + 1:
        return None
    up, dn = np.diff(high), -np.diff(low)
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(high[1:] - low[1:], np.maximum(abs(high[1:] - close[:-1]),
                                                   abs(low[1:] - close[:-1])))
    trs = ema(tr, 2 * n - 1)
    pdi = 100 * ema(plus, 2 * n - 1) / np.where(trs == 0, 1, trs)
    mdi = 100 * ema(minus, 2 * n - 1) / np.where(trs == 0, 1, trs)
    dx = 100 * abs(pdi - mdi) / np.where(pdi + mdi == 0, 1, pdi + mdi)
    return float(ema(dx, 2 * n - 1)[-1])


def log_ret_vol(close, n):
    c = close[-(n + 1):]
    if len(c) < 10:
        return None
    r = np.diff(np.log(c))
    return max(float(r.std(ddof=1)), 1e-6)


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _safe(x):
    return 0.0 if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else float(x)


def build(candles, strike, secs_left, kalshi_bid=None, kalshi_ask=None, vol_per_min=None,
          now_ts=None, avg_seconds=60):
    """Feature dict for one decision moment. candles: 1-minute, oldest first,
    ending at the decision moment. Returns None if there's too little data."""
    if len(candles) < 70 or not strike:
        return None
    c = _arr(candles, "close")
    h, l, v = _arr(candles, "high"), _arr(candles, "low"), _arr(candles, "volume")
    spot = c[-1]
    vol = vol_per_min or log_ret_vol(c, 60)
    if not vol:
        return None
    mins = max(secs_left / 60.0, 1e-3)
    sig_left = vol * math.sqrt(mins)
    now_ts = now_ts or candles[-1]["ts"]
    f = {}

    # the contract
    f["dist_sigma"] = math.log(spot / strike) / sig_left
    # same averaging-window maths the live model uses
    tau, A = secs_left, max(avg_seconds, 1)
    var = (vol ** 2 / 60) * ((tau - A) + A / 3) if tau >= A else (vol ** 2 / 60) * (tau / A) ** 2 * tau / 3
    f["model_prob"] = _norm_cdf(0.9 * math.log(spot / strike) / math.sqrt(max(var, 1e-14)))
    f["min_left"] = mins
    if kalshi_bid is not None and kalshi_ask is not None:
        mid = min(max((kalshi_bid + kalshi_ask) / 2, 0.01), 0.99)
        f["kalshi_logit"] = math.log(mid / (1 - mid))
        f["kalshi_spread"] = kalshi_ask - kalshi_bid
        f["model_minus_kalshi"] = f["model_prob"] - mid

    # momentum, in volatility units, several horizons
    for n in (1, 5, 15, 60):
        f[f"ret_{n}m"] = math.log(c[-1] / c[-1 - n]) / (vol * math.sqrt(n))
    # volatility regime
    v15, v60 = log_ret_vol(c, 15), log_ret_vol(c, 60)
    f["vol_1m"] = vol * 1e4                       # in basis points per minute
    f["vol_ratio_15_60"] = _safe(v15 / v60 if v15 and v60 else None)

    # oscillators, multi-timeframe
    f["rsi_1m"] = _safe(rsi(c, 14))
    f["stochrsi_1m"] = _safe(stoch_rsi(c, 14))
    for tf in (5, 15):
        cs = _arr(resample(candles, tf), "close")
        f[f"rsi_{tf}m"] = _safe(rsi(cs, 14)) if len(cs) > 15 else 50.0
    # trend
    e9, e21 = ema(c, 9)[-1], ema(c, 21)[-1]
    f["ema_9_21"] = (e9 - e21) / spot / vol
    m = ema(c, 12) - ema(c, 26)
    f["macd_hist"] = (m[-1] - ema(m, 9)[-1]) / spot / vol
    c5 = _arr(resample(candles, 5), "close")
    f["ema_trend_5m"] = ((ema(c5, 9)[-1] - ema(c5, 21)[-1]) / spot / vol) if len(c5) >= 21 else 0.0
    f["adx_1m"] = _safe(adx(h, l, c, 14))
    # volatility bands
    w = c[-20:]
    sd = w.std(ddof=1)
    f["bb_pctb"] = float((spot - (w.mean() - 2 * sd)) / (4 * sd)) if sd > 0 else 0.5
    f["bb_width"] = float(4 * sd / spot / vol) if sd > 0 else 0.0
    f["atr_norm"] = _safe(atr(h, l, c, 14)) / spot / vol
    # volume
    day_start = now_ts - now_ts % 86400
    day = [i for i, x in enumerate(candles) if x["ts"] >= day_start]
    if len(day) >= 5 and v[day].sum() > 0:
        vwap = float((c[day] * v[day]).sum() / v[day].sum())
        f["vwap_dist"] = math.log(spot / vwap) / vol
        f["day_high_dist"] = math.log(h[day].max() / spot) / sig_left
        f["day_low_dist"] = math.log(spot / l[day].min()) / sig_left
    else:
        f["vwap_dist"] = f["day_high_dist"] = f["day_low_dist"] = 0.0
    obv = np.cumsum(np.sign(np.diff(c[-31:])) * v[-30:])
    f["obv_slope_15m"] = float((obv[-1] - obv[-16]) / (v[-60:].mean() * 15 + 1e-9))
    f["vol_spike_5m"] = float(v[-5:].mean() / (v[-60:].mean() + 1e-9))
    # round-number magnets ($500 / $1000 for BTC-sized prices)
    step = 10 ** max(0, int(math.log10(spot)) - 2)          # 1000 for $80k, 100 for $3k
    for k, name in ((step / 2, "round_half"), (step, "round_full")):
        nearest = round(spot / k) * k
        f[f"{name}_dist"] = (spot - nearest) / spot / sig_left
    # strike vs round number: is the target sitting on a level?
    f["strike_round_dist"] = (strike - round(strike / step) * step) / strike / sig_left
    # calendar
    hr = (now_ts % 86400) / 3600
    f["hour_sin"], f["hour_cos"] = math.sin(2 * math.pi * hr / 24), math.cos(2 * math.pi * hr / 24)
    f["weekend"] = 1.0 if ((now_ts // 86400) + 4) % 7 >= 5 else 0.0     # 1970-01-01 was a Thursday
    return {k: _safe(x) for k, x in f.items()}


def timeframe_indicator_snapshot(candles, timeframes=(1, 5, 15, 30), now_ts=None):
    """Chart indicators from clock-aligned OHLCV bars without changing ML columns.

    A timeframe is omitted until enough completed bars exist for its indicators.
    In particular, 30m RSI/EMA/MACD/ADX need at least 35 completed 30-minute bars
    (about 17.5 hours of 1m history); insufficient history is never fabricated.
    """
    out = {}
    if not candles:
        return out
    now_ts = float(now_ts) if now_ts is not None else time.time()
    for tf in timeframes:
        bars = list(candles) if tf == 1 else resample(candles, tf)
        # Ignore any currently forming bar so timeframes compare completed candles only.
        bars = [bar for bar in bars if int(bar["ts"]) + tf * 60 <= now_ts]
        if len(bars) < 35:
            continue
        close = _arr(bars, "close")
        high, low, volume = _arr(bars, "high"), _arr(bars, "low"), _arr(bars, "volume")
        spot = float(close[-1])
        if not math.isfinite(spot) or spot <= 0:
            continue
        e9, e21 = ema(close, 9)[-1], ema(close, 21)[-1]
        macd = ema(close, 12) - ema(close, 26)
        macd_hist = (macd[-1] - ema(macd, 9)[-1]) / spot * 10000
        window = close[-20:]
        sd = float(window.std(ddof=1))
        pct_b = float((spot - (window.mean() - 2 * sd)) / (4 * sd)) if sd > 0 else 0.5
        recent_vol = float(volume[-3:].mean())
        baseline_vol = float(volume[-20:].mean())
        out[f"{tf}m"] = {
            "bars": len(bars),
            "rsi": _safe(rsi(close, 14)),
            "ema_9_21_bps": _safe((e9 - e21) / spot * 10000),
            "macd_hist_bps": _safe(macd_hist),
            "adx": _safe(adx(high, low, close, 14)),
            "bb_pct_b": _safe(pct_b),
            "bb_width_pct": _safe(4 * sd / spot * 100),
            "atr_pct": _safe((atr(high, low, close, 14) or 0.0) / spot * 100),
            "volume_ratio": _safe(recent_vol / baseline_vol if baseline_vol > 0 else None),
            "return_3bar_pct": _safe((spot / close[-4] - 1) * 100),
        }
    return out


def names():
    """Stable feature order for the model."""
    demo = [{"ts": 1_700_000_000 + 60 * i, "open": 100 + i * .01, "high": 100.1 + i * .01,
             "low": 99.9 + i * .01, "close": 100 + (i % 7) * .03 + i * .01, "volume": 1 + i % 3}
            for i in range(200)]
    return sorted(build(demo, 101.0, 400, 0.4, 0.42).keys())
