"""Optional Val Town bridge for non-time-critical public Kalshi metadata GETs.

Live order books, trades, WebSockets, private account data, and all writes remain
on the direct Kalshi client. Bridge errors return handled=False for direct fallback.
"""
import os
import re
import requests

_EXACT = {"/events", "/events/multivariate", "/markets", "/series", "/exchange/status", "/historical/cutoff"}
_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

def eligible(path, method="GET"):
    if method.upper() != "GET" or "?" in path:
        return False
    if path in _EXACT:
        return True
    if any(part in path for part in ("/orderbook", "/orderbooks", "/trades", "/portfolio")):
        return False
    parts = path.strip("/").split("/")
    if len(parts) == 2 and parts[0] in ("markets", "events", "series"):
        return bool(_TICKER.fullmatch(parts[1]))
    if len(parts) == 3 and parts[:2] == ["historical", "markets"]:
        return bool(_TICKER.fullmatch(parts[2]))
    return False

def bridge_get(path, params=None, timeout=4):
    """Return (True, dict) on a valid bridge response; otherwise (False, None)."""
    base = (os.environ.get("VAL_TOWN_KALSHI_URL") or "").strip().rstrip("/")
    token = (os.environ.get("ATI_INGEST_SHARED_TOKEN") or "").strip()
    if not base.startswith("https://") or not token or not eligible(path):
        return False, None
    try:
        response = requests.get(base + path, params=params or {},
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=timeout)
        if response.status_code != 200:
            return False, None
        payload = response.json()
        return (True, payload) if isinstance(payload, dict) else (False, None)
    except (requests.RequestException, ValueError):
        return False, None
