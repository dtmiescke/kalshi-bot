"""Free market context beyond price — all public, no keys, all cached.

- Futures funding rate, open interest and basis (Kraken Futures): crowded
  longs/shorts come before squeezes
- Options put/call open-interest ratio (Deribit): demand for downside insurance
- Coinbase vs Kraken price gap: US buying pressure often shows on Coinbase first
- Fear & Greed index (alternative.me): daily crowd mood
- Hyperliquid perps (all four coins, one request): hourly funding and the
  perp-vs-oracle premium, a second derivatives venue that also covers BNB
- Economic calendar (Forex Factory weekly feed): CPI, Fed, jobs, when vol spikes

Each source fails quietly: missing context lowers confidence, it never
breaks a run.
"""
import math
import time
from datetime import datetime

import requests

FUT_TICKERS = "https://futures.kraken.com/derivatives/api/v3/tickers"
FUT_SYMBOL = {"BTC": "PF_XBTUSD", "ETH": "PF_ETHUSD", "SOL": "PF_SOLUSD"}
KRAKEN_SPOT = "https://api.kraken.com/0/public/Ticker"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIR = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
DERIBIT_SUMMARY = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
FNG = "https://api.alternative.me/fng/"
CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"


def _get(url, params=None, timeout=10, get=requests.get):
    try:
        r = get(url, params=params, timeout=timeout, headers={"User-Agent": "kalshi-bot"})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _cached(store, key, minutes, fn):
    """Never recycle expired external evidence when an upstream read fails."""
    now = time.time()
    cached = store.get_kv(key)
    if isinstance(cached, dict):
        try:
            age = now - float(cached["ts"])
            if 0 <= age < max(0, float(minutes)) * 60 and "v" in cached:
                return cached["v"]
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    value = fn()
    if value is not None:
        store.set_kv(key, {"ts": time.time(), "v": value})
    return value


def futures(store, coin, get=requests.get):
    def fetch():
        d = _get(FUT_TICKERS, get=get)
        if not d:
            return None
        t = next((x for x in d.get("tickers", []) if x.get("symbol") == FUT_SYMBOL.get(coin)), None)
        if not t:
            return None
        mark, index = t.get("markPrice"), t.get("indexPrice")
        return {"funding": t.get("fundingRate"), "funding_next": t.get("fundingRatePrediction"),
                "open_interest": t.get("openInterest"),
                "basis": (mark / index - 1) if mark and index else None}
    return _cached(store, f"fut_{coin}", 5, fetch)


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def hyperliquid(store, coin, post=requests.post):
    """Hourly funding, perp premium over the oracle and open interest from
    Hyperliquid's public info API. One cached request serves every coin."""
    def fetch():
        try:
            r = post(HYPERLIQUID_INFO, json={"type": "metaAndAssetCtxs"}, timeout=10,
                     headers={"User-Agent": "kalshi-bot"})
            r.raise_for_status()
            meta, ctxs = r.json()
            out = {}
            for asset, c in zip(meta.get("universe", []), ctxs):
                name = asset.get("name")
                if name in ("BTC", "ETH", "SOL", "BNB") and isinstance(c, dict):
                    out[name] = {"funding": _num(c.get("funding")),
                                 "premium": _num(c.get("premium")),
                                 "open_interest": _num(c.get("openInterest"))}
            return out or None
        except Exception:
            return None
    return (_cached(store, "hl_ctx", 5, fetch) or {}).get(coin) or {}


def oi_change(store, coin, now_oi):
    """Open-interest change since ~1 hour ago (rising OI + move = new money)."""
    hist = store.get_kv(f"oi_hist_{coin}") or []
    now = time.time()
    if now_oi:
        hist = [h for h in hist if now - h[0] < 3 * 3600] + [[now, now_oi]]
        store.set_kv(f"oi_hist_{coin}", hist)
    old = next((h[1] for h in hist if now - h[0] >= 3600), None)
    return (now_oi / old - 1) if (old and now_oi) else None


_SPOT_CACHE = {}


def kraken_spot(coin, get=requests.get, max_age=15):
    hit = _SPOT_CACHE.get(coin)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    d = _get(KRAKEN_SPOT, {"pair": KRAKEN_PAIR.get(coin)}, get=get)
    try:
        res = next(iter(d["result"].values()))
        v = float(res["c"][0])
        _SPOT_CACHE[coin] = (time.time(), v)
        return v
    except (TypeError, KeyError, StopIteration, ValueError):
        return None


def put_call(store, coin, get=requests.get):
    if coin not in ("BTC", "ETH"):
        return None

    def fetch():
        d = _get(DERIBIT_SUMMARY, {"currency": coin, "kind": "option"}, get=get)
        if not d:
            return None
        puts = sum(x.get("open_interest") or 0 for x in d.get("result", [])
                   if str(x.get("instrument_name", "")).endswith("-P"))
        calls = sum(x.get("open_interest") or 0 for x in d.get("result", [])
                    if str(x.get("instrument_name", "")).endswith("-C"))
        return puts / calls if calls else None
    return _cached(store, f"pcr_{coin}", 30, fetch)


def fear_greed(store, get=requests.get):
    def fetch():
        d = _get(FNG, {"limit": 1}, get=get)
        try:
            return int(d["data"][0]["value"])
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    return _cached(store, "fng", 180, fetch)


def calendar(store, get=requests.get):
    """US high/medium impact events this week as [{ts, title, impact}]."""
    def fetch():
        d = _get(CALENDAR, get=get)
        if not isinstance(d, list):
            return None
        out = []
        for e in d:
            if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
                continue
            try:
                ts = datetime.fromisoformat(e["date"]).timestamp()
            except (KeyError, ValueError):
                continue
            out.append({"ts": ts, "title": e.get("title", ""), "impact": e["impact"]})
        return out
    return _cached(store, "calendar", 360, fetch) or []



# Public spot 15-minute candles; Coinbase does not offer the configured BNB-USD product.
COINBASE_15M_CANDLES = "https://api.exchange.coinbase.com/products/{pair}/candles"
COINBASE_SPOT_PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
KRAKEN_15M_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "BNB": "BNBUSD"}


def _completed_15m_candle(rows, source, now=None):
    """Normalize candles and return the newest completed 15-minute bar."""
    now = time.time() if now is None else float(now)
    normalized = []
    for row in rows or []:
        try:
            if source == "coinbase":
                ts, low, high, opn, close, volume = row[:6]
                candle = {"ts": int(ts), "open": float(opn), "high": float(high), "low": float(low), "close": float(close), "volume": float(volume)}
            else:
                ts, opn, high, low, close, _vwap, volume, *_ = row
                candle = {"ts": int(float(ts)), "open": float(opn), "high": float(high), "low": float(low), "close": float(close), "volume": float(volume)}
            if (candle["ts"] + 900 <= now and all(math.isfinite(candle[k]) for k in ("open", "high", "low", "close", "volume")) and min(candle["open"], candle["high"], candle["low"], candle["close"]) > 0 and candle["volume"] >= 0):
                normalized.append(candle)
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    normalized.sort(key=lambda c: c["ts"])
    if not normalized:
        return None
    candle = normalized[-1]
    candle["return_pct"] = (candle["close"] / candle["open"] - 1.0) * 100.0
    candle["age_seconds"] = max(0.0, now - (candle["ts"] + 900))
    candle["source"] = source
    return candle


def _fetch_coinbase_15m(coin, get=requests.get, now=None):
    pair = COINBASE_SPOT_PAIRS.get(coin)
    if not pair:
        return None
    now = time.time() if now is None else float(now)
    end = int(now // 900 * 900)
    start = end - 3 * 900
    def iso(ts):
        return datetime.utcfromtimestamp(ts).isoformat() + "Z"
    try:
        response = get(COINBASE_15M_CANDLES.format(pair=pair), params={"granularity": 900, "start": iso(start), "end": iso(end)}, timeout=6, headers={"User-Agent": "kalshi-bot"})
        response.raise_for_status()
        return _completed_15m_candle(response.json(), "coinbase", now)
    except Exception:
        return None


def _fetch_kraken_15m(coin, get=requests.get, now=None):
    pair = KRAKEN_15M_PAIRS.get(coin)
    if not pair:
        return None
    now = time.time() if now is None else float(now)
    try:
        response = get(KRAKEN_OHLC, params={"pair": pair, "interval": 15}, timeout=6, headers={"User-Agent": "kalshi-bot"})
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            return None
        result = payload.get("result") or {}
        key = next((k for k in result if k != "last"), None)
        return _completed_15m_candle(result.get(key, []) if key else [], "kraken", now)
    except Exception:
        return None


def spot_15m_monitor(store, coin, get=requests.get):
    """Return completed Coinbase/Kraken 15m spot candles, cached for five minutes."""
    coin = str(coin or "").upper()
    if coin not in KRAKEN_15M_PAIRS:
        return {}
    out = {}
    for venue, fetcher in (("coinbase", _fetch_coinbase_15m), ("kraken", _fetch_kraken_15m)):
        if venue == "coinbase" and coin not in COINBASE_SPOT_PAIRS:
            out[venue] = None
            continue
        key = f"spot15_{venue}_{coin}"
        cached = store.get_kv(key)
        now = time.time()
        if isinstance(cached, dict) and 0 <= now - float(cached.get("ts", 0)) < 300 and "v" in cached:
            out[venue] = cached["v"]
        else:
            value = fetcher(coin, get=get)
            # Cache misses too, so an upstream outage does not cause a request every second.
            store.set_kv(key, {"ts": now, "v": value})
            out[venue] = value
    cb, kr = out.get("coinbase"), out.get("kraken")
    out["cross_venue_basis_bps"] = (cb["close"] / kr["close"] - 1.0) * 10000.0 if cb and kr and cb.get("close") and kr.get("close") else None
    out["coin"] = coin
    out["timeframe_minutes"] = 15
    return out

def context(store, coin, coinbase_price=None, get=requests.get, include_spot_15m=False):
    """One compact dict for signals, the council and the report."""
    fut = futures(store, coin, get) or {}
    hl = hyperliquid(store, coin) if get is requests.get else {}
    ks = kraken_spot(coin, get)
    now = time.time()
    upcoming = [e for e in calendar(store, get) if -3600 <= e["ts"] - now <= 6 * 3600]
    from .signals.crypto_microstructure import orderbook as crypto_orderbook
    l2 = crypto_orderbook(store, coin)
    spot15 = spot_15m_monitor(store, coin, get) if include_spot_15m else {}
    cb15 = spot15.get("coinbase") or {}
    kr15 = spot15.get("kraken") or {}
    return {
        "orderbook": l2,
        "spot_15m": spot15,
        "coinbase_15m_return_pct": cb15.get("return_pct"),
        "coinbase_15m_volume": cb15.get("volume"),
        "kraken_15m_return_pct": kr15.get("return_pct"),
        "kraken_15m_volume": kr15.get("volume"),
        "spot_15m_basis_bps": spot15.get("cross_venue_basis_bps"),
        "funding": fut.get("funding"), "basis": fut.get("basis"),
        "oi_change_1h": oi_change(store, coin, fut.get("open_interest")),
        "put_call_oi": put_call(store, coin, get),
        "hl_funding": hl.get("funding"), "hl_premium": hl.get("premium"),
        "hl_open_interest": hl.get("open_interest"),
        "coinbase_premium": (coinbase_price / ks - 1) if (coinbase_price and ks) else None,
        "fear_greed": fear_greed(store, get),
        "events_soon": [f"{e['title']} ({e['impact']}) in {round((e['ts'] - now) / 60)} min"
                        for e in upcoming][:3],
        "_events": upcoming,
    }
