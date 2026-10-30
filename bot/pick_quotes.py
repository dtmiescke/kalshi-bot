"""Current Kalshi prices for every open shadow pick, so the app can show how each bet is doing right now.

One cheap, public read: the open picks' tickers go to Kalshi's markets endpoint in batches. The result replaces a single kv
row; nothing here places or changes a bet."""
import logging
import time

log = logging.getLogger(__name__)

BATCH = 50
TABLES = ("nba_picks", "nfl_picks")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def open_tickers(store):
    out = []
    for table in TABLES:
        try:
            out += [r["ticker"] for r in store.q(f"SELECT ticker FROM {table} WHERE status='open'")]
        except Exception:
            continue                          # a tracker that has not run yet has no table
    return sorted(set(out))


def refresh(store, client, now=None, every=300):
    """Fetch quotes for the open picks at most every `every` seconds. Returns how many tickers were quoted, or None."""
    now = int(time.time() if now is None else now)
    try:
        if now - float(store.get_kv("pick_quotes_last", 0) or 0) < every:
            return None
        store.set_kv("pick_quotes_last", now)
        tickers = open_tickers(store)
        quotes = {}
        for i in range(0, len(tickers), BATCH):
            batch = tickers[i:i + BATCH]
            try:
                rows = client._paged("/markets", "markets", {"tickers": ",".join(batch), "limit": 100}, max_pages=1)
            except Exception as exc:
                log.warning("pick quotes: batch failed: %s", exc)
                continue
            for m in rows:
                bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
                if m.get("ticker") and bid is not None and ask is not None and 0 < ask <= 1:
                    quotes[m["ticker"]] = {"bid": bid, "ask": ask, "ts": now}
        if quotes or not tickers:
            store.set_kv("pick_quotes", quotes)
        return len(quotes)
    except Exception:
        log.warning("pick quotes failed", exc_info=True)
        return None
