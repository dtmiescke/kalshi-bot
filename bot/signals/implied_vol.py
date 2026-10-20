"""Option-implied volatility: what the options market expects, not just what
already happened. Deribit's DVOL index (BTC and ETH) is free and public.

Realized vol looks backward; implied vol looks forward and jumps BEFORE
scheduled events (Fed days, CPI). The brain gets a separate crypto reading
built on implied vol and learns which of the two deserves more trust.
"""
import math
import time

import requests

DVOL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
MINUTES_PER_YEAR = 525600


def fetch_dvol(currency, timeout=10):
    """Latest DVOL (annualised %, e.g. 52.3) or None. Only BTC and ETH exist."""
    if currency not in ("BTC", "ETH"):
        return None
    now = int(time.time() * 1000)
    try:
        r = requests.get(DVOL, timeout=timeout, params={
            "currency": currency, "resolution": "60",
            "start_timestamp": now - 3 * 3600 * 1000, "end_timestamp": now})
        r.raise_for_status()
        data = r.json().get("result", {}).get("data", [])
        return float(data[-1][4]) if data else None
    except (requests.RequestException, ValueError, IndexError, TypeError):
        return None


def dvol_to_per_minute(dvol_pct):
    return dvol_pct / 100.0 / math.sqrt(MINUTES_PER_YEAR) if dvol_pct else None


def cached_dvol(store, currency, fetch=fetch_dvol, refresh_minutes=10):
    key = f"dvol_{currency}"
    c = store.get_kv(key)
    if c and time.time() - c["ts"] < refresh_minutes * 60:
        return c["v"]
    v = fetch(currency)
    if v:
        store.set_kv(key, {"ts": time.time(), "v": v})
        return v
    return c["v"] if c else None
