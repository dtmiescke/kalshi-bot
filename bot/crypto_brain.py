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
    # Cross-venue order-book pressure is a small auxiliary signal only. It is
    # not the Kalshi book and must never outweigh the contract/volatility model.
    book = ctx.get("orderbook") if isinstance(ctx, dict) else None
    if isinstance(book, dict) and isinstance(book.get("depth_imbalance"), (int, float)):
        imbalance = max(-1.0, min(1.0, float(book["depth_imbalance"])))
        spread_bps = max(0.0, float(book.get("spread_bps", 0.0) or 0.0))
        shift = max(-0.02, min(0.02, 0.02 * imbalance))
        book_prob = clamp(p + shift, 0.02, 0.98)
        book_conf = clamp(0.35 * (1.0 - min(spread_bps, 30.0) / 40.0), 0.05, 0.35)
        readings.append(Reading("crypto_orderbook", book_prob, book_conf, {
            "venue": book.get("venue", "coinbase"),
            "depth_imbalance": round(imbalance, 4),
            "spread_bps": round(spread_bps, 3),
            "microprice_delta_bps": round(float(book.get("microprice_delta_bps", 0.0) or 0.0), 3),
            "levels": book.get("levels", 0),
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
    info["indicators"] = indicator_snapshot(feats)
    if isinstance(book, dict):
        info["orderbook"] = {k: book.get(k) for k in (
            "venue", "spread_bps", "depth_imbalance", "microprice_delta_bps", "levels")}
    return readings, info
