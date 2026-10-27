"""One research pipeline for 15-minute crypto contracts, used by BOTH the
12-minute snapshot runs and live streaming, so they can't drift apart.

For one contract at one moment it produces:
- crypto_model     : exact-contract probability on the blended vol FORECAST
- crypto_model_iv  : same, on option-implied (Deribit) vol only
- crypto_momentum  : short-term trend lean
- ml_model         : the trained model's calibrated view of Kalshi's mistakes
                     (only once a model has passed out-of-sample promotion)
...and saves the full feature row so every live decision becomes training data.
"""
import math
import time

from . import features, marketdata, ml, vol as volmod
from .live import settlement
from .signals import implied_vol as iv
from .signals.base import Reading, clamp
from .signals.crypto_microstructure import indicator_snapshot

COIN_OF_SERIES = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXBNB15M": "BNB"}


def strategy_readings(feats, base_prob, chart_indicators=None):
    """Independent, deliberately low-weight crypto strategy views.

    These are candidate signals, not guarantees. Their weights are learned
    against the Kalshi market baseline after settlement by the existing brain.
    """
    if not isinstance(feats, dict) or base_prob is None:
        return []
    out = []

    def val(key, default=0.0):
        try:
            x = float(feats.get(key, default))
            return x if math.isfinite(x) else default
        except (TypeError, ValueError):
            return default

    # Trend continuation: momentum + EMA alignment, gated by trend strength.
    trend_raw = (0.45 * max(-2.0, min(2.0, val("ret_5m"))) / 2
                 + 0.35 * max(-2.0, min(2.0, val("ema_9_21"))) / 2
                 + 0.20 * max(-2.0, min(2.0, val("ema_trend_5m"))) / 2)
    trend_gate = max(0.25, min(1.0, val("adx_1m", 20.0) / 35.0))
    out.append(Reading("crypto_trend_strategy",
                       clamp(base_prob + 0.035 * trend_raw * trend_gate, 0.02, 0.98),
                       0.20 + 0.10 * trend_gate,
                       {"score": round(trend_raw, 4), "adx": round(val("adx_1m"), 2)}))

    # Mean reversion: fade extreme RSI/Bollinger readings only when trend strength
    # is low; the confidence is intentionally capped while this strategy gathers data.
    rsi = val("rsi_1m", 50.0)
    bb = val("bb_pctb", 0.5)
    stretch = max(-1.0, min(1.0, (50.0 - rsi) / 35.0))
    band = max(-1.0, min(1.0, 0.5 - bb))
    reversion = 0.6 * stretch + 0.4 * band
    range_gate = max(0.0, min(1.0, (28.0 - val("adx_1m", 20.0)) / 20.0))
    out.append(Reading("crypto_mean_reversion_strategy",
                       clamp(base_prob + 0.03 * reversion * range_gate, 0.02, 0.98),
                       0.15 + 0.10 * range_gate,
                       {"score": round(reversion, 4), "range_gate": round(range_gate, 4)}))

    # Breakout continuation: only lean into direction when volume confirms proximity
    # to the session high/low. This is a weak supplemental signal, not a trigger.
    hi_dist, lo_dist = val("day_high_dist", 1.0), val("day_low_dist", 1.0)
    near_high = max(0.0, min(1.0, 1.0 - hi_dist / 0.35))
    near_low = max(0.0, min(1.0, 1.0 - lo_dist / 0.35))
    vol_confirm = max(0.0, min(1.0, (val("vol_spike_5m", 1.0) - 1.0) / 1.5))
    breakout = (near_high - near_low) * vol_confirm
    out.append(Reading("crypto_breakout_strategy",
                       clamp(base_prob + 0.025 * breakout, 0.02, 0.98),
                       0.12 + 0.10 * vol_confirm,
                       {"score": round(breakout, 4), "volume_confirm": round(vol_confirm, 4)}))
    # Multi-timeframe chart confirmation. Every timeframe uses completed OHLCV
    # bars; available frames are weighted toward the slower trend to reduce noise.
    chart_indicators = chart_indicators if isinstance(chart_indicators, dict) else {}
    tf_weights = {"1m": 0.15, "5m": 0.20, "15m": 0.30, "30m": 0.35}
    tf_scores, tf_details, total_weight = [], {}, 0.0
    for tf, weight in tf_weights.items():
        ind = chart_indicators.get(tf)
        if not isinstance(ind, dict):
            continue
        try:
            rsi_score = max(-1.0, min(1.0, (float(ind.get("rsi", 50.0)) - 50.0) / 25.0))
            ema_score = max(-1.0, min(1.0, float(ind.get("ema_9_21_bps", 0.0)) / 20.0))
            macd_score = max(-1.0, min(1.0, float(ind.get("macd_hist_bps", 0.0)) / 12.0))
            adx_value = max(0.0, min(1.0, float(ind.get("adx", 0.0)) / 35.0))
            score = (0.35 * ema_score + 0.30 * macd_score + 0.20 * rsi_score) * (0.35 + 0.65 * adx_value)
            tf_scores.append((score, weight))
            total_weight += weight
            tf_details[tf] = {
                "score": round(score, 4), "rsi": round(float(ind.get("rsi", 50.0)), 2),
                "ema_9_21_bps": round(float(ind.get("ema_9_21_bps", 0.0)), 3),
                "macd_hist_bps": round(float(ind.get("macd_hist_bps", 0.0)), 3),
                "adx": round(float(ind.get("adx", 0.0)), 2),
                "bb_pct_b": round(float(ind.get("bb_pct_b", 0.5)), 3),
                "volume_ratio": round(float(ind.get("volume_ratio", 1.0)), 3),
            }
        except (TypeError, ValueError, OverflowError):
            continue
    if tf_scores and total_weight:
        mtf_score = sum(score * weight for score, weight in tf_scores) / total_weight
        mtf_conf = min(0.40, 0.12 + 0.07 * len(tf_scores))
        out.append(Reading("crypto_multi_timeframe_chart",
                           clamp(base_prob + 0.04 * mtf_score, 0.02, 0.98),
                           mtf_conf, {"score": round(mtf_score, 4), "timeframes": tf_details,
                                      "available_timeframes": list(tf_details)}))
    return out


def coin_from_series(series):
    s = (series or "").upper()
    return next((c for c in ("BTC", "ETH", "SOL", "BNB") if s.startswith(f"KX{c}")), "BTC")


def research(store, ticker, series, terms, candles, kalshi_bid, kalshi_ask, now=None,
             spot=None, dvol_fetch=None, with_context=True, log_row=True, context=None):
    """Returns (readings, info). candles: 1-minute OHLCV, oldest first."""
    now = now or time.time()
    coin = COIN_OF_SERIES.get(series) or coin_from_series(series)
    is_15m = str(series).upper().endswith("15M")
    if not (terms.get("strike") and terms.get("close_ts")) or kalshi_bid is None or not kalshi_ask:
        return [], {}
    mid = (kalshi_bid + kalshi_ask) / 2
    readings = [Reading("kalshi_market", clamp(mid), clamp(1 - (kalshi_ask - kalshi_bid) * 5, 0.1, 1.0),
                        {"mid": round(mid, 4), "spread": round(kalshi_ask - kalshi_bid, 4)})]
    if len(candles) < 30:
        return readings, {}
    closes = [c["close"] for c in candles]
    spot = spot or closes[-1]
    secs = terms["close_ts"] - now
    # live mode passes a pre-fetched context so no web request ever runs inside
    # the one-second streaming loop
    ctx = context if context is not None else (marketdata.context(store, coin) if with_context else {})
    dv = iv.cached_dvol(store, coin, fetch=dvol_fetch or iv.fetch_dvol) if coin in ("BTC", "ETH") else None
    sigma, parts = volmod.forecast(store, coin, closes[-120:], dv, now, ctx.get("_events"))
    info = {"coin": coin, "secs_left": secs, "sigma": sigma, "vol_parts": parts,
            "context": {k: v for k, v in ctx.items() if not k.startswith("_")},
            "chart_url": "https://www.tradingview.com/chart/?symbol=COINBASE%3A" + coin + "USD"}
    if not sigma:
        return readings, info

    shrink = volmod.tail_shrink(store, coin)
    # a fitted tail model (Student-t shape + vol scale), once one has proven itself
    tm = volmod.tail_model(store, coin)
    shape = {"tail_df": tm[0], "vol_scale": tm[1]} if tm else {}
    p = settlement.prob_above(spot, terms["strike"], sigma, secs, terms["avg_seconds"],
                              tail_shrink=shrink, **shape)
    if p is None:
        return readings, info
    # the exact z this price came from, so the tail fit uses the same calculation
    z = settlement.z_above(spot, terms["strike"], sigma, secs, terms["avg_seconds"])
    info.update({"tail_shrink": shrink, "live_z": z if z is not None and math.isfinite(z) else None})
    readings.append(Reading("crypto_model", clamp(p, 0.02, 0.98), 0.75 if secs > 120 else 0.5,
                            {"spot": spot, "strike": terms["strike"], "secs_left": round(secs),
                             "sigma": round(sigma, 7), "avg_s": terms["avg_seconds"],
                             "season": parts.get("season"), "event_mult": parts.get("event_mult"),
                             "tail_shrink": shrink, "tail_df": shape.get("tail_df"),
                             "vol_scale": shape.get("vol_scale", 1.0),
                             "z": None if info["live_z"] is None
                             else round(info["live_z"], 5)}))
    # Cross-venue L2 is a directional input, not a substitute for Kalshi's
    # executable book. Combine depth imbalance and size-weighted microprice;
    # cap the effect so a noisy snapshot cannot overwhelm the contract model.
    book = ctx.get("orderbook") if isinstance(ctx, dict) else None
    if isinstance(book, dict) and isinstance(book.get("depth_imbalance"), (int, float)):
        imbalance = max(-1.0, min(1.0, float(book["depth_imbalance"])))
        spread_bps = max(0.0, float(book.get("spread_bps", 0.0) or 0.0))
        micro_bps = max(-20.0, min(20.0, float(book.get("microprice_delta_bps", 0.0) or 0.0)))
        # Microprice is in basis points; combine it with depth imbalance and
        # cap the adjustment at 3 probability points. Historical results learn its weight.
        shift = max(-0.03, min(0.03, 0.02 * imbalance + 0.0005 * micro_bps))
        book_prob = clamp(p + shift, 0.02, 0.98)
        levels = max(0, int(book.get("levels", 0) or 0))
        quality = min(1.0, levels / 5.0) if levels else 0.5
        book_conf = clamp(0.4 * (1.0 - min(spread_bps, 30.0) / 40.0) * quality,
                          0.05, 0.4)
        readings.append(Reading("crypto_orderbook", book_prob, book_conf, {
            "venue": book.get("venue", "coinbase"),
            "depth_imbalance": round(imbalance, 4),
            "spread_bps": round(spread_bps, 3),
            "microprice_delta_bps": round(micro_bps, 3),
            "levels": levels,
            "cross_venue_proxy": True,
        }))

    if dv:
        pi = settlement.prob_above(spot, terms["strike"], iv.dvol_to_per_minute(dv), secs,
                                   terms["avg_seconds"], tail_shrink=shrink,
                                   tail_df=shape.get("tail_df"))
        if pi is not None:
            readings.append(Reading("crypto_model_iv", clamp(pi, 0.02, 0.98), 0.6, {"dvol": dv}))
    if len(closes) > 6:
        z = math.log(closes[-1] / closes[-6]) / (sigma * math.sqrt(5))
        readings.append(Reading("crypto_momentum", clamp(p + 0.04 * max(-2, min(2, z)), 0.02, 0.98),
                                clamp(abs(z) / 2, 0.05, 0.6), {"z_5m": round(z, 3)}))

    feats = features.build(candles[-300:], terms["strike"], secs, kalshi_bid, kalshi_ask,
                           vol_per_min=None, now_ts=int(now), avg_seconds=terms["avg_seconds"])
    # the trained model only knows 15-minute windows: never let it price an
    # hourly or daily contract it has never seen, and don't mix those rows in
    if feats and is_15m:
        pm, meta = ml.predict(feats, coin)
        if pm is not None:
            gain = max(0.0, 1 - meta["test_logloss_model"] / meta["test_logloss_kalshi"])
            readings.append(Reading("ml_model", clamp(pm, 0.02, 0.98), clamp(0.5 + gain * 20, 0.5, 0.95),
                                    {"trained": meta.get("trained"), "test_gain": round(gain, 4)}))
        if log_row and 2 * 60 <= secs <= 13 * 60:
            # one row per contract per minute; context saved for future model versions
            row = dict(feats)
            row.update({f"ctx_{k}": v for k, v in info["context"].items()
                        if isinstance(v, (int, float)) and v is not None})
            # not model inputs (features.names() ignores them): the live pricing
            # z and tail value, for vol.learn_tail_shrink
            row.update({"live_z": info["live_z"], "tail_shrink_used": shrink})
            ml.add_row(store, ticker, int(now) - int(now) % 60, coin, row, None, "live")
    info["features"] = feats
    chart_indicators = features.timeframe_indicator_snapshot(candles, now_ts=now)
    info["chart_indicators"] = chart_indicators
    # These strategy views are available to the same ensemble/settlement learner
    # as existing signals. Low initial weights prevent unvalidated heuristics from
    # overpowering the exact-contract model or Kalshi's executable market price.
    if feats:
        readings.extend(strategy_readings(feats, p, chart_indicators))
    info["indicators"] = indicator_snapshot(feats)
    if isinstance(book, dict):
        info["orderbook"] = {k: book.get(k) for k in (
            "venue", "spread_bps", "depth_imbalance", "microprice_delta_bps", "levels")}
    return readings, info
