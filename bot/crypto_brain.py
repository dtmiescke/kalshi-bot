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

COIN_OF_SERIES = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL"}


def coin_from_series(series):
    s = (series or "").upper()
    return next((c for c in ("BTC", "ETH", "SOL") if s.startswith(f"KX{c}")), "BTC")


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
            "context": {k: v for k, v in ctx.items() if not k.startswith("_")}}
    if not sigma:
        return readings, info

    shrink = volmod.tail_shrink(store, coin)
    p = settlement.prob_above(spot, terms["strike"], sigma, secs, terms["avg_seconds"],
                              tail_shrink=shrink)
    if p is None:
        return readings, info
    readings.append(Reading("crypto_model", clamp(p, 0.02, 0.98), 0.75 if secs > 120 else 0.5,
                            {"spot": spot, "strike": terms["strike"], "secs_left": round(secs),
                             "sigma": round(sigma, 7), "avg_s": terms["avg_seconds"],
                             "season": parts.get("season"), "event_mult": parts.get("event_mult")}))
    if dv:
        pi = settlement.prob_above(spot, terms["strike"], iv.dvol_to_per_minute(dv), secs,
                                   terms["avg_seconds"], tail_shrink=shrink)
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
            ml.add_row(store, ticker, int(now) - int(now) % 60, coin, row, None, "live")
    info["features"] = feats
    return readings, info
