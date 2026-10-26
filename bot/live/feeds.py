"""Live WebSocket feeds. All free:

- Kraken (public): Level-2 book (100 levels) + every executed trade
- Coinbase (public): ticker, used for the multi-exchange composite price
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


def handle_kalshi(msg, books, ticker_updates=None):
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
            ticker_updates[ticker] = {
                "market_ticker": ticker,
                **{key: body[key] for key in fields if key in body},
                "received_ts": time.time(),
            }
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


def kalshi_subs(tickers):
    if not tickers:
        return []
    return [{"id": 1, "cmd": "subscribe",
             "params": {"channels": ["orderbook_delta", "ticker"],
                        "market_tickers": tickers}}]
