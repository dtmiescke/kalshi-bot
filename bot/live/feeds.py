"""Live WebSocket feeds. All free:

- Kraken (public): Level-2 book (100 levels) + every executed trade
- Coinbase (public): ticker, used for the multi-exchange composite price
- Bitstamp, Gemini (public): top of book, also in the composite. Kalshi settles
  15-minute markets on CF Benchmarks' Real-Time Indices, built from several
  constituent exchanges (these four among them), so a median across them
  tracks the settlement price better than any one venue.
- Kalshi (needs your free API key): contract order-book changes

Parsing is kept in small pure functions (handle_*) so it can be tested
without a network. Connections reconnect with backoff and count every
reconnect as a data-quality "gap".
"""
import asyncio
import json
import logging
import time
from datetime import datetime

log = logging.getLogger("bot.live")

KRAKEN_WS = "wss://ws.kraken.com/v2"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BITSTAMP_WS = "wss://ws.bitstamp.net"
GEMINI_WS = "wss://api.gemini.com/v2/marketdata"
KALSHI_WS = {"demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
             "production": "wss://api.elections.kalshi.com/trade-api/ws/v2"}


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return time.time()


# --- pure handlers -----------------------------------------------------------------
def handle_kraken(msg, analytics_by_symbol):
    """msg: decoded Kraken v2 message. analytics_by_symbol: {"BTC/USD": SymbolAnalytics}"""
    ch, typ = msg.get("channel"), msg.get("type")
    if ch == "instrument":
        for p in (msg.get("data") or {}).get("pairs", []):
            a = analytics_by_symbol.get(p.get("symbol"))
            if a and p.get("price_precision") is not None:
                a.book.precision = (int(p["price_precision"]), int(p["qty_precision"]))
        return
    if ch == "book":
        for d in msg.get("data", []):
            a = analytics_by_symbol.get(d.get("symbol"))
            if not a:
                continue
            ts = _ts(d.get("timestamp")) if d.get("timestamp") else time.time()
            if typ == "snapshot":
                a.book.snapshot([(l["price"], l["qty"]) for l in d.get("bids", [])],
                                [(l["price"], l["qty"]) for l in d.get("asks", [])], ts)
            else:
                for l in d.get("bids", []):
                    a.book.update("bid", l["price"], l["qty"], ts)
                for l in d.get("asks", []):
                    a.book.update("ask", l["price"], l["qty"], ts)
            # raises ChecksumError on mismatch -> the feed reconnects for a snapshot
            a.book.verify(d.get("checksum"))
    elif ch == "trade":
        for t in msg.get("data", []):
            a = analytics_by_symbol.get(t.get("symbol"))
            if a:
                a.add_trade(_ts(t.get("timestamp")), t["price"], t["qty"], t.get("side"))


def handle_coinbase(msg, analytics_by_product):
    """Coinbase ticker -> reference price; Coinbase matches -> live Coinbase
    trades, used to build candles IDENTICAL to the ones the model trained on."""
    a = analytics_by_product.get(msg.get("product_id"))
    if not a:
        return
    if msg.get("type") == "ticker" and msg.get("price"):
        a.add_reference("coinbase", _ts(msg.get("time")), msg["price"])
    elif msg.get("type") in ("match", "last_match") and msg.get("price"):
        a.add_cb_trade(_ts(msg.get("time")), msg["price"], msg.get("size", 0))


def handle_bitstamp(msg, analytics_by_pair):
    """Bitstamp order_book_<pair> snapshot (top 100 levels) -> mid as a reference price."""
    ch = str(msg.get("channel") or "")
    if msg.get("event") != "data" or not ch.startswith("order_book_"):
        return
    a = analytics_by_pair.get(ch[len("order_book_"):])
    d = msg.get("data") or {}
    try:
        bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return
    if a and 0 < bid < ask:
        ts = int(d.get("microtimestamp") or 0) / 1e6 or time.time()
        a.add_reference("bitstamp", ts, (bid + ask) / 2)


class GeminiBooks:
    """Gemini's l2 feed sends a full book, then level changes (~50/s for BTC).
    Keep each side as {price: qty} and track the best level incrementally;
    rescan a side only when its best level is removed."""

    def __init__(self):
        self.books = {}      # symbol -> {"buy": {}, "sell": {}, "best": {"buy": p, "sell": p}}
        self.last_emit = {}

    def apply(self, symbol, changes):
        b = self.books.setdefault(symbol, {"buy": {}, "sell": {}, "best": {"buy": None, "sell": None}})
        for side, price, qty in changes:
            if side not in ("buy", "sell"):
                continue
            px, q = float(price), float(qty)
            levels, best = b[side], b["best"][side]
            better = (lambda x, y: x > y) if side == "buy" else (lambda x, y: x < y)
            if q > 0:
                levels[px] = q
                if best is None or better(px, best):
                    b["best"][side] = px
            else:
                levels.pop(px, None)
                if px == best:
                    b["best"][side] = (max(levels) if side == "buy" else min(levels)) if levels else None
        bid, ask = b["best"]["buy"], b["best"]["sell"]
        return (bid + ask) / 2 if bid and ask and bid < ask else None


def handle_gemini(msg, analytics_by_symbol, books, now=None, every=0.25):
    """Gemini v2 l2_updates -> top-of-book mid as a reference price (throttled)."""
    if msg.get("type") != "l2_updates":
        return
    sym = msg.get("symbol")
    a = analytics_by_symbol.get(sym)
    if not a:
        return
    try:
        mid = books.apply(sym, msg.get("changes") or [])
    except (TypeError, ValueError):
        return
    now = time.time() if now is None else now
    if mid and now - books.last_emit.get(sym, 0) >= every:
        books.last_emit[sym] = now
        a.add_reference("gemini", now, mid)


def handle_kalshi(msg, books, ticker_updates=None, ticker_update_history=None):
    """Update order books and the latest ticker snapshot from Kalshi messages."""
    typ, body = msg.get("type"), msg.get("msg", {})
    ticker = body.get("market_ticker")
    if typ == "ticker":
        if ticker_updates is not None and ticker:
            # Keep a small, numeric/diagnostic-only record for the dashboard.
            fields = ("price_dollars", "yes_bid_dollars", "yes_ask_dollars",
                      "volume_fp", "open_interest_fp", "dollar_volume",
                      "dollar_open_interest", "yes_bid_size_fp", "yes_ask_size_fp",
                      "last_trade_size_fp", "ts", "ts_ms", "time")
            received = time.time()
            row = {
                "market_ticker": ticker,
                **{key: body[key] for key in fields if key in body},
                "received_ts": received,
            }
            try:
                bid = float(body.get("yes_bid_dollars"))
                ask = float(body.get("yes_ask_dollars"))
                if 0 <= bid <= 1 and 0 <= ask <= 1 and ask >= bid:
                    row["yes_mid"] = (bid + ask) / 2.0
            except (TypeError, ValueError):
                pass
            ticker_updates[ticker] = row
            if ticker_update_history is not None and row.get("yes_mid") is not None:
                history = ticker_update_history.setdefault(ticker, [])
                history.append({"received_ts": received, "yes_mid": row["yes_mid"]})
                if len(history) > 2000:
                    del history[:-2000]
        return
    book = books.get(ticker)
    if not book:
        return
    if typ == "orderbook_snapshot":
        book.snapshot(body, msg.get("seq"))
    elif typ == "orderbook_delta":
        book.delta(body, msg.get("seq"))


# --- connection loops ----------------------------------------------------------------
async def _connect(url, headers=None):
    import websockets  # imported lazily so tests run without it
    try:
        return await websockets.connect(url, additional_headers=headers or {},
                                        ping_interval=20, max_size=2 ** 22)
    except TypeError:   # older websockets versions
        return await websockets.connect(url, extra_headers=headers or {},
                                        ping_interval=20, max_size=2 ** 22)


async def run_feed(name, url, subscribe_msgs, handler, on_gap, stop, headers_fn=None,
                    on_connect=None):
    """Generic reconnecting loop. subscribe_msgs: callable -> list of dicts."""
    backoff = 1
    while not stop.is_set():
        try:
            ws = await _connect(url, headers_fn() if headers_fn else None)
            async with ws:
                for m in subscribe_msgs():
                    await ws.send(json.dumps(m))
                backoff = 1
                if on_connect is not None:
                    on_connect()
                log.info("%s connected", name)
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    handler(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            on_gap()
            log.warning("%s feed dropped (%s); reconnecting in %ss", name, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def kraken_subs(symbols, depth=100):
    return [{"method": "subscribe", "params": {"channel": "instrument", "snapshot": True}},
            {"method": "subscribe", "params": {"channel": "book", "symbol": symbols, "depth": depth}},
            {"method": "subscribe", "params": {"channel": "trade", "symbol": symbols}}]


def coinbase_subs(products):
    return [{"type": "subscribe", "product_ids": products, "channels": ["ticker", "matches"]}]


def bitstamp_subs(pairs):
    return [{"event": "bts:subscribe", "data": {"channel": f"order_book_{p}"}} for p in pairs]


def gemini_subs(symbols):
    return [{"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": list(symbols)}]}]


def kalshi_subs(tickers):
    if not tickers:
        return []
    return [{"id": 1, "cmd": "subscribe",
             "params": {"channels": ["orderbook_delta", "ticker"],
                        "market_tickers": tickers}}]
