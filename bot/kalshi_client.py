"""Kalshi Trade API v2 client.

Auth: every private request is signed with RSA-PSS (SHA-256) over
    timestamp_ms + METHOD + path   (path includes /trade-api/v2, no query string)
and sent as KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP / KALSHI-ACCESS-SIGNATURE.

Kalshi has been moving price fields from integer cents (yes_bid) to dollar
strings (yes_bid_dollars). `price()` reads either, always returning dollars 0-1.
"""
import base64
import math
import re
import threading
import time
import uuid
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def price(obj, field):
    """Return a validated 0..1 dollar price from Kalshi dollar or cent fields.

    Reject malformed, non-finite, and out-of-range data rather than passing an
    impossible quote to research, sizing, or execution. If both formats are
    supplied, prefer the newer dollar field but require consistency within one
    cent to avoid silently accepting contradictory exchange data.
    """
    if not isinstance(obj, dict):
        return None
    raw_dollars = obj.get(f"{field}_dollars")
    raw_cents = obj.get(field)

    def _parse(value, scale):
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            amount = float(value) / scale
        except (TypeError, ValueError, OverflowError):
            return None
        return amount if math.isfinite(amount) and 0.0 <= amount <= 1.0 else None

    has_dollars = raw_dollars not in (None, "")
    has_cents = raw_cents not in (None, "")
    dollars = _parse(raw_dollars, 1.0) if has_dollars else None
    cents = _parse(raw_cents, 100.0) if has_cents else None
    if (has_dollars and dollars is None) or (has_cents and cents is None):
        return None
    if has_dollars and has_cents and abs(dollars - cents) > 0.010000001:
        return None
    return dollars if has_dollars else cents


def num(obj, field):
    for k in (f"{field}_fp", field):
        v = obj.get(k)
        if v not in (None, ""):
            return float(v)
    return 0.0


# One cooldown and one pace for EVERY client in the process: if Kalshi says
# "slow down", all callers slow down together instead of each hammering it.
_GLOBAL = {"cooldown_until": 0.0, "last_request": 0.0, "lock": threading.Lock(), "log": []}
MIN_INTERVAL = 0.12        # at most ~8 requests/second, well under Kalshi's limits
CACHE_TTL = {"/events": 20, "/markets": 15, "/series": 300, "/exchange": 30}


def _cache_ttl(path):
    """Prices (order books, trades) are NEVER cached: an old price must not look
    fresh. Lists of markets/events change slowly and are cached briefly."""
    if "/orderbook" in path or "/trades" in path or path.startswith("/portfolio"):
        return 0
    for prefix, ttl in CACHE_TTL.items():
        if path.startswith(prefix):
            return ttl
    return 0


def request_log():
    """Recent responses (status, Retry-After, rate-limit headers) for diagnosis."""
    return list(_GLOBAL["log"])


class KalshiClient:
    def __init__(self, base_url, key_id=None, private_key_pem=None, timeout=15):
        self.base_url = base_url.rstrip("/")
        self.path_prefix = urlparse(self.base_url).path  # e.g. /trade-api/v2
        self.key_id = key_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kalshi-bot/1.0 (research; contact via repo)"
        self._cache = {}
        self._key = None
        if private_key_pem:
            pem = private_key_pem.replace("\\n", "\n").encode()
            self._key = serialization.load_pem_private_key(pem, password=None)

    # --- signing -----------------------------------------------------------
    def _headers(self, method, path):
        if not (self._key and self.key_id):
            return {}
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + self.path_prefix + path.split("?")[0]).encode()
        sig = self._key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def _req(self, method, path, params=None, body=None, retries=3):
        # POST/DELETE writes may succeed even if the response is lost. Never
        # blindly retry a non-idempotent request after an ambiguous failure.
        if method.upper() != "GET":
            retries = 1
        url = self.base_url + path
        ttl = _cache_ttl(path) if method == "GET" else 0
        ck = (path, tuple(sorted((params or {}).items())))
        if ttl:
            hit = self._cache.get(ck)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        last = None
        for attempt in range(retries):
            self._pace()
            r = self.session.request(method, url, params=params, json=body,
                                     headers=self._headers(method, path),
                                     timeout=self.timeout)
            self._note(method, path, r)
            last = r
            if r.status_code == 429:
                # Respect Kalshi: wait exactly as long as it asks (Retry-After), for
                # EVERY caller. Never rotate hosts or proxies to dodge a limit.
                wait = self._retry_after(r) or min(60.0, 2.0 * (2 ** attempt))
                _GLOBAL["cooldown_until"] = max(_GLOBAL["cooldown_until"], time.time() + wait)
                if wait > 20:      # too long to wait inside a run: give up cleanly
                    break
                continue
            r.raise_for_status()
            data = r.json() if r.content else {}
            if ttl:
                self._cache[ck] = (time.time(), data)
            return data
        last.raise_for_status()

    @staticmethod
    def _retry_after(r):
        v = r.headers.get("Retry-After")
        if not v:
            return None
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
            except Exception:
                return None

    @staticmethod
    def _pace():
        with _GLOBAL["lock"]:
            now = time.time()
            wait = max(_GLOBAL["cooldown_until"] - now,
                       _GLOBAL["last_request"] + MIN_INTERVAL - now, 0.0)
            if wait:
                time.sleep(wait)
            _GLOBAL["last_request"] = time.time()

    @staticmethod
    def _note(method, path, r):
        _GLOBAL["log"] = (_GLOBAL["log"] + [{
            "ts": round(time.time(), 2), "method": method, "path": path.split("?")[0],
            "status": r.status_code, "retry_after": r.headers.get("Retry-After"),
            "ratelimit": {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()},
            "signed": "KALSHI-ACCESS-KEY" in (r.request.headers if r.request else {}),
        }])[-50:]

    def _paged(self, path, key, params, max_pages=20):
        out, cursor = [], None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._req("GET", path, params=p)
            out.extend(data.get(key, []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    # --- market data (public) ----------------------------------------------
    def sports_events(self, series_ticker, status="open"):
        """Sports games must be pulled by series ticker; keyword search returns parlays."""
        return self._paged("/events", "events", {
            "series_ticker": series_ticker, "status": status,
            "with_nested_markets": "true", "limit": 200})

    def sports_series(self):
        """Discover Kalshi series classified under the Sports category.

        Catalog metadata is cached briefly; callers must still validate open
        events/markets and must not infer that every series is trade-ready.
        """
        rows = self._paged("/series", "series", {
            "category": "Sports", "include_volume": "true", "limit": 1000,
        }, max_pages=10)
        out = {}
        for row in rows:
            ticker = row.get("ticker")
            if not ticker:
                continue
            # The server-side category filter is the source of truth. Do not
            # reject valid series just because a response omits category metadata.
            out[ticker] = row
        return out

    def sports_events_all(self, status="open"):
        """Fetch a bounded, paginated set of open events across the catalog.

        This avoids making one API call per sports series. The result is bounded
        intentionally; callers should report truncation/coverage uncertainty.
        """
        return self._paged("/events", "events", {
            "status": status, "with_nested_markets": "true", "limit": 200,
        }, max_pages=20)

    def multivariate_events(self, status="open"):
        """Read-only discovery for Kalshi's multivariate/combo event catalog."""
        out, cursor = [], None
        for _ in range(10):
            params = {"status": status, "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            data = self._req("GET", "/events/multivariate", params=params)
            rows = data.get("events") or data.get("multivariate_events") or []
            out.extend(rows)
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def market(self, ticker):
        return self._req("GET", f"/markets/{ticker}").get("market", {})

    def series(self, ticker):
        """Read official series settlement sources, restrictions, and document links."""
        ticker = str(ticker or "").strip()
        if not ticker or "/" in ticker or "?" in ticker:
            raise ValueError("invalid Kalshi series ticker")
        return self._req("GET", f"/series/{ticker}").get("series", {})

    def contract_context(self, ticker):
        """Read-only contract rules and settlement-source metadata.

        Reuses this client's existing paced, cached public GET requests.
        No order access, PDF execution, or independent MCP trading process.
        """
        from bot.contract_intelligence import contract_context
        return contract_context(self, ticker)

    def orderbook(self, ticker, depth=20):
        """Returns {'yes': [(price, qty), ...], 'no': [...]} as bids, best first."""
        data = self._req("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        ob = data.get("orderbook_fp") or data.get("orderbook") or {}
        out = {}
        for side in ("yes", "no"):
            levels = ob.get(f"{side}_dollars") or ob.get(side) or []
            parsed = []
            for lvl in levels:
                p, qty = float(lvl[0]), float(lvl[1])
                if p > 1.0:          # cents format
                    p /= 100.0
                parsed.append((p, qty))
            out[side] = sorted(parsed, key=lambda x: -x[0])
        return out

    def market_orderbooks(self, tickers):
        """Fetch 1-100 market order books in one official bulk REST request.

        Returns {ticker: {'yes': [(price_dollars, qty), ...],
                          'no': [(price_dollars, qty), ...]}}.
        Prices are normalized to dollars, levels best-first. Read-only.
        """
        if not isinstance(tickers, (list, tuple)) or not 1 <= len(tickers) <= 100:
            raise ValueError("tickers must be a list of 1-100 market tickers")
        clean = []
        for ticker in tickers:
            if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", ticker):
                raise ValueError("invalid market ticker")
            if ticker not in clean:
                clean.append(ticker)
        if not clean:
            raise ValueError("at least one unique ticker is required")
        data = self._req("GET", "/markets/orderbooks", params={"tickers": clean})
        out = {}
        for row in data.get("orderbooks", []):
            ticker = row.get("ticker")
            ob = row.get("orderbook_fp") or row.get("orderbook") or {}
            sides = {}
            for side in ("yes", "no"):
                levels = ob.get(f"{side}_dollars") or ob.get(side) or []
                parsed = []
                for level in levels:
                    if not isinstance(level, (list, tuple)) or len(level) < 2:
                        continue
                    try:
                        p, qty = float(level[0]), float(level[1])
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if p > 1.0:
                        p /= 100.0
                    if math.isfinite(p) and 0.0 <= p <= 1.0 and math.isfinite(qty) and qty >= 0:
                        parsed.append((p, qty))
                sides[side] = sorted(parsed, key=lambda x: -x[0])
            if isinstance(ticker, str):
                out[ticker] = sides
        return out

    def place_order_v2(self, ticker, side, contracts, limit_price, *,
                       time_in_force="immediate_or_cancel", post_only=False,
                       expiration_time=None, cancel_order_on_pause=False,
                       reduce_only=False):
        """Submit a V2 order using the documented YES-book schema.

        side is 'bid' (buy YES) or 'ask' (sell YES), not legacy YES/NO.
        Kept separate until callers and fill reconciliation are migrated.
        """
        if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", ticker):
            raise ValueError("invalid market ticker")
        if side not in ("bid", "ask"):
            raise ValueError("side must be bid or ask")
        if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts <= 0:
            raise ValueError("contracts must be a positive integer")
        if isinstance(limit_price, bool) or not isinstance(limit_price, (int, float)):
            raise ValueError("limit_price must be numeric")
        if not math.isfinite(limit_price) or not 0.01 <= limit_price <= 0.99:
            raise ValueError("limit_price must be finite and between $0.01 and $0.99")
        price_dollars = f"{limit_price:.4f}"
        if abs(float(price_dollars) - limit_price) > 1e-8:
            raise ValueError("limit_price must be representable to four decimal places")
        if time_in_force not in ("fill_or_kill", "good_till_canceled", "immediate_or_cancel"):
            raise ValueError("unsupported V2 time_in_force")
        if not isinstance(post_only, bool) or not isinstance(cancel_order_on_pause, bool) or not isinstance(reduce_only, bool):
            raise ValueError("V2 order flags must be booleans")
        if expiration_time is not None and (
            isinstance(expiration_time, bool) or not isinstance(expiration_time, int) or expiration_time <= int(time.time())
        ):
            raise ValueError("expiration_time must be a future Unix timestamp")
        if expiration_time is not None and time_in_force != "good_till_canceled":
            raise ValueError("expiration_time requires good_till_canceled")
        body = {
            "ticker": ticker, "client_order_id": str(uuid.uuid4()), "side": side,
            "count": f"{contracts:.2f}", "price": price_dollars,
            "time_in_force": time_in_force, "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only, "cancel_order_on_pause": cancel_order_on_pause,
            "reduce_only": reduce_only, "subaccount": 0, "exchange_index": 0,
        }
        if expiration_time is not None:
            body["expiration_time"] = expiration_time
        return self._req("POST", "/portfolio/events/orders", body=body)

    def trades(self, ticker, min_ts=None, limit=500):
        params = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self._paged("/markets/trades", "trades", params, max_pages=5)

    # --- portfolio (signed) --------------------------------------------------
    def balance(self):
        return self._req("GET", "/portfolio/balance")

    def positions(self):
        return self._req("GET", "/portfolio/positions")

    @staticmethod
    def _validated_order(ticker, side, contracts, limit_price):
        """Reject malformed order inputs before sending an authenticated request."""
        if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", ticker):
            raise ValueError("invalid market ticker")
        if side not in ("yes", "no"):
            raise ValueError("side must be yes or no")
        if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts <= 0:
            raise ValueError("contracts must be a positive integer")
        if isinstance(limit_price, bool) or not isinstance(limit_price, (int, float)):
            raise ValueError("limit_price must be numeric")
        if not math.isfinite(limit_price) or not 0.01 <= limit_price <= 0.99:
            raise ValueError("limit_price must be finite and between $0.01 and $0.99")
        cents = round(limit_price * 100)
        if abs(limit_price * 100 - cents) > 1e-6:
            raise ValueError("limit_price must be on a one-cent tick")
        return int(cents)

    def place_resting_buy(self, ticker, side, contracts, limit_price, expires_s=60):
        """Maker order: rests on the book at limit_price until filled, cancelled
        or expired (so a forgotten order can never linger)."""
        cents = self._validated_order(ticker, side, contracts, limit_price)
        if isinstance(expires_s, bool) or not isinstance(expires_s, int) or not 1 <= expires_s <= 3600:
            raise ValueError("expires_s must be an integer between 1 and 3600")
        body = {"ticker": ticker, "action": "buy", "side": side, "count": int(contracts),
                "type": "limit", "client_order_id": str(uuid.uuid4()),
                "expiration_ts": int(time.time()) + int(expires_s),
                ("yes_price" if side == "yes" else "no_price"): cents}
        return self._req("POST", "/portfolio/orders", body=body).get("order", {})

    def order(self, order_id):
        return self._req("GET", f"/portfolio/orders/{order_id}").get("order", {})

    def cancel(self, order_id):
        return self._req("DELETE", f"/portfolio/orders/{order_id}")

    def place_limit_sell(self, ticker, side, contracts, limit_price):
        """Sell contracts we hold, immediate-or-cancel at limit_price or better."""
        cents = self._validated_order(ticker, side, contracts, limit_price)
        body = {"ticker": ticker, "action": "sell", "side": side, "count": int(contracts),
                "type": "limit", "client_order_id": str(uuid.uuid4()),
                "time_in_force": "immediate_or_cancel",
                ("yes_price" if side == "yes" else "no_price"): cents}
        return self._req("POST", "/portfolio/orders", body=body).get("order", {})

    def place_limit_buy(self, ticker, side, contracts, limit_price):
        """Limit buy at `limit_price` dollars. Immediate-or-cancel so the bot never
        leaves stale resting orders when it isn't watching."""
        cents = self._validated_order(ticker, side, contracts, limit_price)
        body = {
            "ticker": ticker, "action": "buy", "side": side, "count": int(contracts),
            "type": "limit", "client_order_id": str(uuid.uuid4()),
            "time_in_force": "immediate_or_cancel",
            ("yes_price" if side == "yes" else "no_price"): cents,
        }
        return self._req("POST", "/portfolio/orders", body=body).get("order", {})

    # Official Kalshi live evidence; read-only and advisory, never execution authority.
    @staticmethod
    def _safe_identifier(value):
        import re
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
            raise ValueError("invalid Kalshi identifier")
        return value

    def sports_filters(self):
        return self._req("GET", "/search/filters_by_sport")

    def milestone_live_data(self, milestone_id):
        ident = self._safe_identifier(milestone_id)
        return self._req("GET", f"/live_data/milestone/{ident}")

    def milestone_game_stats(self, milestone_id):
        ident = self._safe_identifier(milestone_id)
        return self._req("GET", f"/live_data/milestone/{ident}/game_stats")

    def event_live_data(self, event_ticker):
        ident = self._safe_identifier(event_ticker)
        return self._req("GET", f"/live_data/events/{ident}")
