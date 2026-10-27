"""Optional, credit-conscious Val Town bridge for public Kalshi metadata only.

Live books/trades, WebSockets, private account data, and every write stay direct.
Successful metadata responses are cached briefly across client instances in this
process. Repeated bridge failures trigger a short circuit-breaker cooldown so the
bot immediately falls back to its direct Kalshi client instead of burning Val
Town executions on an outage. This cache is never used for prices/order books.
"""
import json
import os
import re
import threading
import time
from collections import OrderedDict

import requests

_EXACT = {"/events", "/events/multivariate", "/markets", "/series", "/exchange/status", "/historical/cutoff"}
_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_CACHE_MAX = 256
_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 30.0
_CACHE_TTL = (
    ("/series", 300.0),
    ("/historical/", 120.0),
    ("/exchange/status", 15.0),
    ("/events", 15.0),
    ("/markets", 15.0),
)
_LOCK = threading.RLock()
_CACHE = OrderedDict()
_FAILURES = 0
_COOLDOWN_UNTIL = 0.0


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


def _ttl(path):
    # This bridge is never eligible for execution prices or live books.
    for prefix, seconds in _CACHE_TTL:
        if path == prefix or path.startswith(prefix + "/") or prefix.endswith("/") and path.startswith(prefix):
            return seconds
    return 0.0


def _cache_key(base, path, params):
    # Stable key across dict ordering; values are serialized only for keying,
    # never altered in the request itself.
    normalized = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    return (base, path, normalized)


def _cached(key, now):
    item = _CACHE.get(key)
    if item is None:
        return None
    expires, payload = item
    if now >= expires:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return payload


def _record_failure(now):
    global _FAILURES, _COOLDOWN_UNTIL
    with _LOCK:
        _FAILURES += 1
        if _FAILURES >= _FAILURE_THRESHOLD:
            _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, now + _COOLDOWN_SECONDS)


def _record_success():
    global _FAILURES, _COOLDOWN_UNTIL
    with _LOCK:
        _FAILURES = 0
        _COOLDOWN_UNTIL = 0.0


def _reset_for_tests():
    """Reset process-local bridge state; used only by unit tests."""
    global _FAILURES, _COOLDOWN_UNTIL
    with _LOCK:
        _CACHE.clear()
        _FAILURES = 0
        _COOLDOWN_UNTIL = 0.0


def bridge_get(path, params=None, timeout=4):
    """Return (True, dict) on a valid bridge response; otherwise (False, None).

    Cached responses are restricted to non-time-critical public metadata. The
    cooldown is a cost/availability optimization, not a data-validity signal.
    """
    base = (os.environ.get("VAL_TOWN_KALSHI_URL") or "").strip().rstrip("/")
    token = (os.environ.get("ATI_INGEST_SHARED_TOKEN") or "").strip()
    if not base.startswith("https://") or not token or not eligible(path):
        return False, None

    ttl = _ttl(path)
    key = _cache_key(base, path, params)
    now = time.monotonic()
    with _LOCK:
        cached = _cached(key, now) if ttl else None
        if cached is not None:
            return True, cached
        if now < _COOLDOWN_UNTIL:
            return False, None

    try:
        response = requests.get(
            base + path, params=params or {},
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            timeout=max(0.1, min(float(timeout), 4.0)),
        )
        if response.status_code != 200:
            _record_failure(time.monotonic())
            return False, None
        payload = response.json()
        if not isinstance(payload, dict):
            _record_failure(time.monotonic())
            return False, None
        _record_success()
        if ttl:
            with _LOCK:
                _CACHE[key] = (time.monotonic() + ttl, payload)
                _CACHE.move_to_end(key)
                while len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
        return True, payload
    except (requests.RequestException, ValueError, TypeError, OverflowError):
        _record_failure(time.monotonic())
        return False, None
