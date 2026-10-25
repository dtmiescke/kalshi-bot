"""Free market context beyond price — all public, no keys, all cached.

- Futures funding rate, open interest and basis (Kraken Futures): crowded
  longs/shorts come before squeezes
- Options put/call open-interest ratio (Deribit): demand for downside insurance
- Coinbase vs Kraken price gap: US buying pressure often shows on Coinbase first
- Fear & Greed index (alternative.me): daily crowd mood
- Economic calendar (Forex Factory weekly feed): CPI, Fed, jobs, when vol spikes

Each source fails quietly: missing context lowers confidence, it never
breaks a run.
"""
import time
from datetime import datetime

import requests

FUT_TICKERS = "https://futures.kraken.com/derivatives/api/v3/tickers"
FUT_SYMBOL = {"BTC": "PF_XBTUSD", "ETH": "PF_ETHUSD", "SOL": "PF_SOLUSD"}
KRAKEN_SPOT = "https://api.kraken.com/0/public/Ticker"
KRAKEN_PAIR = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
DERIBIT_SUMMARY = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
FNG = "https://api.alternative.me/fng/"
CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _get(url, params=None, timeout=10, get=requests.get):
    try:
        r = get(url, params=params, timeout=timeout, headers={"User-Agent": "kalshi-bot"})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _cached(store, key, minutes, fn):
    """Return fresh optional evidence only; never recycle expired research data.

    On upstream failure an expired value is unknown, not a current signal.
    The cache is retained on disk for diagnostics but is not exposed to trading.
    """
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


def context(store, coin, coinbase_price=None, get=requests.get):
    """One compact dict for signals, the council and the report."""
    fut = futures(store, coin, get) or {}
    ks = kraken_spot(coin, get)
    now = time.time()
    upcoming = [e for e in calendar(store, get) if -3600 <= e["ts"] - now <= 6 * 3600]
    from .signals.crypto_microstructure import orderbook as crypto_orderbook
    l2 = crypto_orderbook(store, coin)
    return {
        "orderbook": l2,
        "funding": fut.get("funding"), "basis": fut.get("basis"),
        "oi_change_1h": oi_change(store, coin, fut.get("open_interest")),
        "put_call_oi": put_call(store, coin, get),
        "coinbase_premium": (coinbase_price / ks - 1) if (coinbase_price and ks) else None,
        "fear_greed": fear_greed(store, get),
        "events_soon": [f"{e['title']} ({e['impact']}) in {round((e['ts'] - now) / 60)} min"
                        for e in upcoming][:3],
        "_events": upcoming,
    }
