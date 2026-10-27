"""Kalshi 15-minute crypto Up/Down markets (KXBTC15M etc.).

Each window asks: will the coin finish above the "price to beat" (its price
when the window opened)? With the live spot price, the strike, the minutes
left and recent volatility, the chance of finishing above is close to a
textbook calculation. Edge appears when Kalshi's price lags a fast move.

Caveat: Kalshi settles on its own reference index (CF Benchmarks). We use
Coinbase's public price as a close proxy, so tiny gaps near the strike are noise.
"""
import math
import re
import time
from datetime import datetime, timezone

import requests

from .base import Reading, clamp

COINBASE = "https://api.exchange.coinbase.com/products/{pair}/candles"


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


_CANDLE_CACHE = {}
CANDLE_CACHE_SECONDS = 45
CANDLE_HISTORY_MINUTES = 1160

def fetch_candles(pair, timeout=10):
    """Fetch ~19 hours of 1-minute OHLCV in API-safe chunks, oldest first."""
    now = time.time()
    cached = _CANDLE_CACHE.get(pair)
    if cached and now - cached[0] < CANDLE_CACHE_SECONDS:
        return list(cached[1])
    try:
        # Coinbase limits each candle response to roughly 300 rows.
        end_ts = int(now // 60 * 60)
        rows_by_ts = {}
        chunk_minutes = 290
        chunks = (CANDLE_HISTORY_MINUTES + chunk_minutes - 1) // chunk_minutes
        for chunk in range(chunks):
            end = end_ts - chunk * chunk_minutes * 60
            start = end - chunk_minutes * 60
            start_iso = datetime.fromtimestamp(start, timezone.utc).isoformat().replace("+00:00", "Z")
            end_iso = datetime.fromtimestamp(end, timezone.utc).isoformat().replace("+00:00", "Z")
            response = requests.get(COINBASE.format(pair=pair),
                                    params={"granularity": 60, "start": start_iso, "end": end_iso},
                                    timeout=timeout, headers={"User-Agent": "kalshi-bot"})
            response.raise_for_status()
            for row in response.json():
                ts = int(row[0])
                rows_by_ts[ts] = {"ts": ts, "low": float(row[1]), "high": float(row[2]),
                                  "open": float(row[3]), "close": float(row[4]),
                                  "volume": float(row[5])}
        rows = [rows_by_ts[k] for k in sorted(rows_by_ts)]
        _CANDLE_CACHE[pair] = (now, rows)
        return list(rows)
    except (requests.RequestException, ValueError, IndexError, TypeError, OverflowError):
        # Do not return old cached candles as if they were fresh after failure.
        return []

KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
_KRAKEN_CANDLE_CACHE = {}


def _kraken_result_pair(result):
    """Return the first pair key, excluding Kraken's pagination cursor."""
    return next((key for key in result if key != "last"), None)


def fetch_kraken_candles(pair, timeout=10):
    """Fetch up to Kraken's public 720 one-minute OHLC bars for a pair like BNB/USD."""
    now = time.time()
    cached = _KRAKEN_CANDLE_CACHE.get(pair)
    if cached and now - cached[0] < CANDLE_CACHE_SECONDS:
        return list(cached[1])
    try:
        response = requests.get(KRAKEN_OHLC, params={
            "pair": str(pair).replace("/", ""), "interval": 1
        }, timeout=timeout, headers={"User-Agent": "kalshi-bot"})
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            return []
        result = payload.get("result") or {}
        key = _kraken_result_pair(result)
        if not key:
            return []
        rows = []
        for row in result[key]:
            ts, opn, high, low, close, _vwap, volume, *_ = row
            values = [float(opn), float(high), float(low), float(close), float(volume)]
            if not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or volume < 0:
                continue
            rows.append({"ts": int(ts), "open": values[0], "high": values[1],
                         "low": values[2], "close": values[3], "volume": values[4]})
        rows.sort(key=lambda row: row["ts"])
        _KRAKEN_CANDLE_CACHE[pair] = (now, rows)
        return list(rows)
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return []


def fetch_kraken_spot(pair, timeout=6):
    """Latest Kraken last-trade price for a pair like BNB/USD; no stale fallback."""
    try:
        response = requests.get(KRAKEN_TICKER, params={"pair": str(pair).replace("/", "")},
                                timeout=timeout, headers={"User-Agent": "kalshi-bot"})
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            return None, None
        result = payload.get("result") or {}
        key = _kraken_result_pair(result)
        price = float(result[key]["c"][0]) if key else None
        if price is None or not math.isfinite(price) or price <= 0:
            return None, None
        return price, time.time()
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return None, None


TICKER = "https://api.exchange.coinbase.com/products/{pair}/ticker"


def fetch_spot(pair, timeout=6):
    """Latest TRADE price and its timestamp. Candle closes can be a minute old,
    which against a live Kalshi book creates fake edges, so snapshot runs use this."""
    try:
        r = requests.get(TICKER.format(pair=pair), timeout=timeout,
                         headers={"User-Agent": "kalshi-bot"})
        r.raise_for_status()
        d = r.json()
        from datetime import datetime
        ts = datetime.fromisoformat(d["time"].replace("Z", "+00:00")).timestamp()
        return float(d["price"]), ts
    except Exception:
        return None, None


def strike_of(mkt):
    """The window's price to beat. Kalshi exposes it as floor_strike; fall back
    to the first dollar amount in the market's text."""
    for k in ("floor_strike", "cap_strike"):
        v = mkt.get(k)
        if v not in (None, ""):
            return float(v)
    text = " ".join(str(mkt.get(k, "")) for k in ("yes_sub_title", "subtitle", "title"))
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def minute_vol(candles, lookback=60):
    closes = [c["close"] for c in candles[-(lookback + 1):]]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 20:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return max(math.sqrt(var), 1e-5)


def finish_above(spot, strike, sigma_min, minutes_left):
    """P(price at close > strike) under a driftless random walk, with z shrunk
    10% because crypto has fatter tails than the normal curve assumes."""
    if minutes_left <= 0:
        return 1.0 if spot > strike else 0.0
    z = math.log(spot / strike) / (sigma_min * math.sqrt(minutes_left))
    return norm_cdf(0.9 * z)


def crypto_model(mkt, candles, minutes_left):
    strike = strike_of(mkt)
    sig = minute_vol(candles)
    if not (strike and sig and candles):
        return None
    spot = candles[-1]["close"]
    p = finish_above(spot, strike, sig, minutes_left)
    # stale price data = low confidence
    conf = 0.75 if minutes_left >= 2 else 0.5
    return Reading("crypto_model", clamp(p, 0.02, 0.98), conf, {
        "spot": spot, "strike": strike, "sigma_min": round(sig, 6),
        "minutes_left": round(minutes_left, 2)})


def crypto_momentum(candles, base_prob, minutes=5):
    """Short-term trend. Whether 5-minute momentum carries into the next few
    minutes is exactly what the brain will test and weight."""
    if len(candles) < minutes + 21 or base_prob is None:
        return None
    sig = minute_vol(candles)
    move = math.log(candles[-1]["close"] / candles[-1 - minutes]["close"])
    strength = move / (sig * math.sqrt(minutes))
    shift = 0.04 * max(-2.0, min(2.0, strength))
    return Reading("crypto_momentum", clamp(base_prob + shift, 0.02, 0.98),
                   clamp(abs(strength) / 2, 0.05, 0.6),
                   {"move_5m": round(move, 5), "z": round(strength, 3)})
