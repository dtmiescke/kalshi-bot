"""Explainable candle-pattern and indicator confluence for short-window crypto.

This module describes observed setups; it deliberately emits no win probability.
The existing calibrated model, Kalshi book/depth, fees, and risk gates remain
responsible for trade selection.
"""
import math
from statistics import mean


def _ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1 - alpha) * out[-1])
    return out


def _rsi(closes, period=14):
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(0.0, x) for x in changes[-period:]]
    losses = [max(0.0, -x) for x in changes[-period:]]
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def analyze_chart_setups(candles, book_imbalance=None, aggressor_imbalance=None):
    """Return indicators, named setups and confluence score from OHLCV candles.

    candles are chronological dictionaries containing open/high/low/close/volume.
    Setup score is a rule-based descriptive score, not a calibrated probability.
    """
    bars = [b for b in (candles or []) if all(
        isinstance(b.get(k), (int, float)) and math.isfinite(float(b[k]))
        for k in ("open", "high", "low", "close", "volume")
    ) and b["close"] > 0 and b["high"] >= b["low"]]
    if len(bars) < 20:
        return {"ready": False, "bars": len(bars), "reason": "need at least 20 valid candles",
                "setups": [], "confluence_score": 0, "indicators": {}}

    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    volumes = [max(0.0, float(b["volume"])) for b in bars]
    last = bars[-1]
    c, o, h, l = map(float, (last["close"], last["open"], last["high"], last["low"]))
    ema9, ema21, ema50 = _ema(closes, 9)[-1], _ema(closes, 21)[-1], _ema(closes, 50)[-1]
    rsi = _rsi(closes)
    macd_line = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    macd_signal = _ema(macd_line, 9)
    macd_hist = macd_line[-1] - macd_signal[-1]
    recent20 = closes[-20:]
    mean20 = mean(recent20)
    sd20 = math.sqrt(mean((x - mean20) ** 2 for x in recent20))
    upper, lower = mean20 + 2 * sd20, mean20 - 2 * sd20
    bb_pctb = (c - lower) / (upper - lower) if upper > lower else 0.5
    true_ranges = []
    for i in range(max(1, len(bars) - 14), len(bars)):
        prev = closes[i - 1]
        true_ranges.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    atr = mean(true_ranges) if true_ranges else h - l
    prior_high, prior_low = max(highs[-21:-1]), min(lows[-21:-1])
    vol_base = mean(volumes[-21:-1])
    typical_prices = [(float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0 for b in bars]
    total_volume = sum(volumes[-20:])
    vwap20 = sum(p * v for p, v in zip(typical_prices[-20:], volumes[-20:])) / total_volume if total_volume > 0 else mean(closes[-20:])
    roc10 = (c / closes[-11] - 1.0) * 100.0 if len(closes) >= 11 and closes[-11] else 0.0
    stoch_low, stoch_high = min(lows[-14:]), max(highs[-14:])
    stoch_k = (c - stoch_low) / (stoch_high - stoch_low) * 100.0 if stoch_high > stoch_low else 50.0
    plus_dm, minus_dm = [], []
    for i in range(max(1, len(bars) - 14), len(bars)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(max(0.0, up_move) if up_move > down_move else 0.0)
        minus_dm.append(max(0.0, down_move) if down_move > up_move else 0.0)
    plus_di = sum(plus_dm) / max(atr, 1e-12) * 100.0
    minus_di = sum(minus_dm) / max(atr, 1e-12) * 100.0
    adx_proxy = abs(plus_di - minus_di) / max(plus_di + minus_di, 1e-12) * 100.0
    volume_ratio = volumes[-1] / vol_base if vol_base > 0 else None
    body = abs(c - o)
    candle_range = max(h - l, 1e-12)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    setups = []
    def add(name, direction, strength, evidence):
        setups.append({"name": name, "direction": direction,
                       "strength": round(max(0.0, min(1.0, strength)), 2),
                       "evidence": evidence})

    if l < prior_low and c > prior_low:
        add("downside_liquidity_sweep_reclaim", "up", 0.65, {"swept_prior_low": True, "close_reclaimed_level": True})
    if h > prior_high and c < prior_high:
        add("upside_liquidity_sweep_rejection", "down", 0.65, {"swept_prior_high": True, "close_rejected_level": True})
    if c > prior_high:
        add("range_breakout", "up", 0.7 if (volume_ratio or 0) >= 1.5 else 0.5,
            {"close_above_prior_20_high": True, "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None})
    elif c < prior_low:
        add("range_breakdown", "down", 0.7 if (volume_ratio or 0) >= 1.5 else 0.5,
            {"close_below_prior_20_low": True, "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None})
    if ema9 > ema21 > ema50:
        add("ema_stack_trend", "up", 0.7, {"ema9": round(ema9, 6), "ema21": round(ema21, 6), "ema50": round(ema50, 6)})
    elif ema9 < ema21 < ema50:
        add("ema_stack_trend", "down", 0.7, {"ema9": round(ema9, 6), "ema21": round(ema21, 6), "ema50": round(ema50, 6)})
    if lower_wick / candle_range >= 0.55 and c >= o:
        add("bullish_rejection_wick", "up", 0.55, {"lower_wick_fraction": round(lower_wick / candle_range, 2)})
    if upper_wick / candle_range >= 0.55 and c <= o:
        add("bearish_rejection_wick", "down", 0.55, {"upper_wick_fraction": round(upper_wick / candle_range, 2)})
    if len(bars) >= 2:
        prev = bars[-2]
        if c > o and prev["close"] < prev["open"] and o <= prev["close"] and c >= prev["open"]:
            add("bullish_engulfing", "up", 0.6, {"body_engulfs_previous": True})
        elif c < o and prev["close"] > prev["open"] and o >= prev["close"] and c <= prev["open"]:
            add("bearish_engulfing", "down", 0.6, {"body_engulfs_previous": True})
    if sd20 / mean20 < 0.003 if mean20 else False:
        add("bollinger_squeeze", "neutral", 0.45, {"band_width_pct": round((upper-lower)/mean20*100, 3)})
    if c > vwap20 and roc10 > 0:
        add("vwap_momentum_alignment", "up", 0.45, {"vwap20": round(vwap20, 6), "roc10_pct": round(roc10, 3)})
    elif c < vwap20 and roc10 < 0:
        add("vwap_momentum_alignment", "down", 0.45, {"vwap20": round(vwap20, 6), "roc10_pct": round(roc10, 3)})
    if volume_ratio is not None and volume_ratio >= 2.0 and abs(roc10) >= 0.15:
        add("volume_momentum_burst", "up" if roc10 > 0 else "down", 0.6,
            {"volume_ratio": round(volume_ratio, 2), "roc10_pct": round(roc10, 3)})
    if rsi is not None and rsi <= 30:
        add("rsi_oversold", "up", 0.4, {"rsi14": round(rsi, 1)})
    elif rsi is not None and rsi >= 70:
        add("rsi_overbought", "down", 0.4, {"rsi14": round(rsi, 1)})
    if book_imbalance is not None and abs(float(book_imbalance)) >= 0.25:
        add("orderbook_imbalance", "up" if book_imbalance > 0 else "down",
            min(0.8, 0.45 + abs(float(book_imbalance)) / 2),
            {"imbalance": round(float(book_imbalance), 3)})
    if aggressor_imbalance is not None and abs(float(aggressor_imbalance)) >= 0.2:
        add("aggressive_trade_flow", "up" if aggressor_imbalance > 0 else "down",
            min(0.8, 0.45 + abs(float(aggressor_imbalance)) / 2),
            {"imbalance": round(float(aggressor_imbalance), 3)})

    up = sum(x["strength"] for x in setups if x["direction"] == "up")
    down = sum(x["strength"] for x in setups if x["direction"] == "down")
    total = up + down
    bias = "up" if up > down else "down" if down > up else "mixed"
    indicators = {
        "ema9": round(ema9, 6), "ema21": round(ema21, 6), "ema50": round(ema50, 6),
        "rsi14": round(rsi, 2) if rsi is not None else None,
        "macd_hist": round(macd_hist, 8), "bb_pctb": round(bb_pctb, 3),
        "atr14": round(atr, 8), "adx_proxy14": round(adx_proxy, 2),
        "stochastic_k14": round(stoch_k, 2), "vwap20": round(vwap20, 8),
        "roc10_pct": round(roc10, 4), "prior_20_high": round(prior_high, 8),
        "prior_20_low": round(prior_low, 8),
        "volume_ratio_20": round(volume_ratio, 3) if volume_ratio is not None else None,
        "book_imbalance": round(float(book_imbalance), 3) if book_imbalance is not None else None,
        "aggressor_imbalance": round(float(aggressor_imbalance), 3) if aggressor_imbalance is not None else None,
    }
    return {"ready": True, "bars": len(bars), "bias": bias,
            "confluence_score": round((abs(up - down) / max(total, 1e-9)) * 100, 1) if total else 0,
            "up_score": round(up, 2), "down_score": round(down, 2),
            "setups": setups, "indicators": indicators,
            "warning": "Rule-based setup confluence is not a win probability; validate by walk-forward results."}
