"""Cached, read-only crypto microstructure for Kalshi 15-minute markets.

Coinbase Level-2 depth is a cross-venue proxy, NOT Kalshi liquidity or a
guaranteed predictor. It is deliberately a low-weight auxiliary reading.
Existing OHLCV indicators remain in bot.features so train/live feature order
stays stable. No orders are placed here.
"""
import math
import time

import requests

BOOK_URL = "https://api.exchange.coinbase.com/products/{pair}/book"
PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
CACHE_SECONDS = 5


def summarize_book(payload, depth=10):
    """Return robust top-of-book/depth metrics from Coinbase L2 JSON."""
    if not isinstance(payload, dict):
        return None
    try:
        bids = [(float(row[0]), float(row[1])) for row in payload.get("bids", [])[:depth]
                if len(row) >= 2 and float(row[0]) > 0 and float(row[1]) > 0]
        asks = [(float(row[0]), float(row[1])) for row in payload.get("asks", [])[:depth]
                if len(row) >= 2 and float(row[0]) > 0 and float(row[1]) > 0]
        if not bids or not asks:
            return None
        bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
        if ask <= bid:
            return None
        bid_depth = sum(size for _, size in bids)
        ask_depth = sum(size for _, size in asks)
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total else 0.0
        # Size-weighted microprice. Keep this as a market diagnostic, not a
        # direct price target: queue position and cross-venue basis differ.
        best_bid_size = next((s for p, s in bids if p == bid), 0.0)
        best_ask_size = next((s for p, s in asks if p == ask), 0.0)
        denom = best_bid_size + best_ask_size
        microprice = ((ask * best_bid_size + bid * best_ask_size) / denom
                      if denom else (bid + ask) / 2)
        mid = (bid + ask) / 2
        return {
            "venue": "coinbase", "bid": bid, "ask": ask, "mid": mid,
            "spread_bps": (ask - bid) / mid * 10000,
            "bid_depth": bid_depth, "ask_depth": ask_depth,
            "depth_imbalance": imbalance,
            "microprice_delta_bps": (microprice / mid - 1) * 10000,
            "levels": min(len(bids), len(asks)),
        }
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return None


def orderbook(store, coin, get=requests.get, now=None, timeout=4):
    """Cached public Level-2 book. Failure is non-fatal and returns None."""
    coin = (coin or "").upper()
    pair = PAIRS.get(coin)
    if not pair:
        return None
    now = time.time() if now is None else now
    key = f"crypto_l2_{coin}"
    cached = store.get_kv(key)
    if isinstance(cached, dict) and now - cached.get("ts", 0) < CACHE_SECONDS:
        return cached.get("value")
    try:
        response = get(BOOK_URL.format(pair=pair), params={"level": 2}, timeout=timeout,
                       headers={"User-Agent": "kalshi-bot"})
        response.raise_for_status()
        result = summarize_book(response.json())
        if result:
            store.set_kv(key, {"ts": now, "value": result})
            return result
    except Exception:
        # Public data can be unavailable or rate limited; use a still-fresh
        # cached value only. Never fabricate a book or block the trading loop.
        if isinstance(cached, dict) and now - cached.get("ts", 0) < 30:
            return cached.get("value")
    return None



def summarize_kalshi_book(book, depth=10):
    """Summarize Kalshi YES/NO bid depth returned by KalshiClient.orderbook."""
    if not isinstance(book, dict):
        return None
    try:
        yes = [(float(p), float(q)) for p, q in (book.get("yes") or [])[:depth]
               if float(p) >= 0 and float(q) > 0]
        no = [(float(p), float(q)) for p, q in (book.get("no") or [])[:depth]
              if float(p) >= 0 and float(q) > 0]
        yes_depth, no_depth = sum(q for _, q in yes), sum(q for _, q in no)
        total = yes_depth + no_depth
        if not total:
            return None
        return {
            "venue": "kalshi", "yes_bid_depth": yes_depth, "no_bid_depth": no_depth,
            "depth_imbalance": (yes_depth - no_depth) / total,
            "yes_levels": len(yes), "no_levels": len(no),
            "best_yes_bid": yes[0][0] if yes else None,
            "best_no_bid": no[0][0] if no else None,
        }
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None

def indicator_snapshot(features):
    """Compact diagnostic snapshot from the existing, stable feature pipeline."""
    if not isinstance(features, dict):
        return {}
    keys = (
        "ret_1m", "ret_5m", "ret_15m", "ret_60m", "rsi_1m", "rsi_5m",
        "rsi_15m", "ema_9_21", "ema_trend_5m", "macd_hist", "adx_1m",
        "bb_pctb", "bb_width", "atr_norm", "vwap_dist", "obv_slope_15m",
        "vol_spike_5m", "vol_ratio_15_60", "dist_sigma", "model_minus_kalshi",
    )
    return {key: features[key] for key in keys if key in features}
