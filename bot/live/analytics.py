"""Turns raw streaming updates into the compact numbers the council reads.

Refreshed about once per second. Everything here is plain arithmetic, so it's
fast; the slow LLM council only ever sees these summaries, never raw ticks.
"""
import math
import os as _os
import time
from collections import deque


class SymbolAnalytics:
    def __init__(self, symbol, book):
        self.symbol = symbol
        self.book = book
        self.trades = deque(maxlen=20000)     # (ts, price, qty, side)
        self.mids = deque(maxlen=4 * 3600)    # (ts, mid) once per second
        self.other_prices = {}                # exchange -> (ts, price) for the composite
        self.gaps = 0                         # feed problems seen (sequence gaps, reconnects)
        self.cb_trades = deque(maxlen=50000)  # Coinbase trades: (ts, price, size)
        self.price_events = deque(maxlen=20000)  # observed receive-time price events for lead/lag study

    # --- inputs ------------------------------------------------------------------
    def add_trade(self, ts, price, qty, side):
        self.trades.append((ts, float(price), float(qty), side))

    def add_cb_trade(self, ts, price, size):
        self.cb_trades.append((ts, float(price), float(size)))

    def add_reference(self, exchange, ts, price):
        price = float(price)
        self.other_prices[exchange] = (ts, price)
        received = time.time()
        if math.isfinite(price) and price > 0:
            self.price_events.append({"source": str(exchange), "event_ts": float(ts),
                                      "received_ts": received, "price": price})

    def sample(self, now=None):
        now = now or time.time()
        m = self.book.mid()
        if m:
            self.mids.append((now, m))
            self.price_events.append({"source": "kraken", "event_ts": now,
                                      "received_ts": now, "price": float(m)})

    # --- helpers -----------------------------------------------------------------
    def _mid_ago(self, seconds, now):
        target = now - seconds
        for ts, m in reversed(self.mids):
            if ts <= target:
                return m
        return None

    def returns(self, now):
        m = self.book.mid()
        out = {}
        for label, s in (("10s", 10), ("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600)):
            past = self._mid_ago(s, now)
            out[label] = math.log(m / past) if (m and past) else None
        return out

    def realized_vol_per_min(self, now, window=1800):
        """Std-dev of 1-minute log returns over the window."""
        pts = [m for ts, m in self.mids if ts >= now - window]
        mins = pts[::60]
        rets = [math.log(b / a) for a, b in zip(mins, mins[1:]) if a and b]
        if len(rets) < 5:
            return None
        mu = sum(rets) / len(rets)
        # floor: a perfectly flat stretch must not read as "zero risk"
        return max(math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)), 1e-5)

    def flow(self, now, window=60):
        recent = [t for t in self.trades if t[0] >= now - window]
        buy = sum(q * p for ts, p, q, s in recent if s == "buy")
        sell = sum(q * p for ts, p, q, s in recent if s == "sell")
        tot = buy + sell
        return {"buy_usd": round(buy), "sell_usd": round(sell),
                "aggressor_imbalance": round((buy - sell) / tot, 3) if tot else 0.0,
                "trades": len(recent)}

    def fresh_source_prices(self, now=None, max_age=5):
        """Return timestamp-fresh source prices, labelled by venue.

        The Kraken local book mid is a source; other_prices contains independent
        reference venues such as Coinbase. Stale prices are excluded rather than
        silently reused in the consensus.
        """
        now = time.time() if now is None else float(now)
        out = {
            name: float(price)
            for name, (ts, price) in self.other_prices.items()
            if price is not None and 0 <= now - float(ts) <= max_age
            and math.isfinite(float(price)) and float(price) > 0
        }
        mid = self.book.mid()
        book_ts = getattr(self.book, "last_update", 0) or 0
        if mid and 0 <= now - book_ts <= max_age and math.isfinite(float(mid)) and float(mid) > 0:
            out["kraken"] = float(mid)
        return out

    def source_divergence_bps(self, now=None, max_age=5):
        """Full cross-venue price spread in basis points; None with <2 sources."""
        prices = list(self.fresh_source_prices(now, max_age).values())
        if len(prices) < 2:
            return None
        prices.sort()
        mid = len(prices) // 2
        center = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
        if center <= 0:
            return None
        return (max(prices) - min(prices)) / center * 10000

    def source_consensus(self, now=None, max_age=5, max_divergence_bps=25):
        """Explain cross-venue agreement without treating it as a trade signal.

        A score is available only when at least two independent fresh prices
        exist. It is deterministic diagnostics, not a probability of winning.
        """
        sources = self.fresh_source_prices(now, max_age)
        prices = sorted(sources.values())
        if len(prices) < 2:
            return {
                "source_count": len(prices),
                "median_price": prices[0] if prices else None,
                "spread_bps": None,
                "median_abs_deviation_bps": None,
                "agreement_score": None,
            }
        mid = len(prices) // 2
        median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
        deviations = sorted(abs(price - median) for price in prices)
        dev_mid = len(deviations) // 2
        mad = deviations[dev_mid] if len(deviations) % 2 else (deviations[dev_mid - 1] + deviations[dev_mid]) / 2
        spread = (prices[-1] - prices[0]) / median * 10000 if median else None
        mad_bps = mad / median * 10000 if median else None
        # 100 means tight agreement; 0 means spread is at/above the configured
        # safety threshold. Do not let more venues mask a bad disagreement.
        threshold = max(float(max_divergence_bps), 0.01)
        score = max(0.0, min(100.0, 100.0 * (1.0 - float(spread or 0.0) / threshold)))
        return {
            "source_count": len(prices),
            "median_price": median,
            "spread_bps": spread,
            "median_abs_deviation_bps": mad_bps,
            "agreement_score": round(score, 1),
        }

    def composite_price(self, now, max_age=5):
        """Median of fresh independent exchange prices, excluding stale feeds."""
        prices = sorted(self.fresh_source_prices(now, max_age).values())
        if not prices:
            return None
        n = len(prices)
        return prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2

    # --- the summary ---------------------------------------------------------------
    def summary(self, now=None):
        now = now or time.time()
        bid1, ask1 = self.book.depth_within(0.1)
        bid5, ask5 = self.book.depth_within(0.5)
        recent_trades = [{"price": p} for ts, p, q, s in self.trades if ts >= now - 30]
        self.book.track_walls(recent_trades, now=now)
        r = self.returns(now)
        vol = self.realized_vol_per_min(now)
        f60 = self.flow(now, 60)
        f300 = self.flow(now, 300)
        regime = "unknown"
        if vol and r.get("15m") is not None:
            trend = abs(r["15m"]) / (vol * math.sqrt(15))
            regime = "trending" if trend > 1.5 else "ranging" if trend < 0.5 else "mixed"
            v5 = self.realized_vol_per_min(now, 300)
            if v5 and v5 > 2.5 * vol:
                regime = "unstable"
        age = now - self.book.last_update if self.book.last_update else None
        # if Kraken and Coinbase disagree by a lot, one feed is broken or lagging:
        # trust neither until they agree again
        div = None
        cb = self.other_prices.get("coinbase")
        m = self.book.mid()
        if cb and m and 0 <= now - cb[0] <= 5:
            div = abs(cb[1] / m - 1) * 1e4
        fresh_sources = self.fresh_source_prices(now, max_age=5)
        source_div = self.source_divergence_bps(now, max_age=5)
        consensus = self.source_consensus(now, max_age=5, max_divergence_bps=float(_os.environ.get("MAX_FEED_DIVERGENCE_BPS", 25)))
        max_div = float(_os.environ.get("MAX_FEED_DIVERGENCE_BPS", 25))
        return {
            "symbol": self.symbol, "mid": self.book.mid(),
            "composite": self.composite_price(now),
            "source_prices": {k: round(v, 8) for k, v in fresh_sources.items()},
            "source_divergence_bps": round(source_div, 2) if source_div is not None else None,
            "source_consensus": consensus,
            "spread_bps": round(self.book.spread_bps() or 0, 2),
            "returns": {k: (round(v, 5) if v is not None else None) for k, v in r.items()},
            "vol_per_min": round(vol, 6) if vol else None, "regime": regime,
            "depth_usd": {"bid_0.1%": round(bid1), "ask_0.1%": round(ask1),
                          "bid_0.5%": round(bid5), "ask_0.5%": round(ask5)},
            "book_imbalance_0.1%": round(self.book.imbalance(0.1), 3),
            "flow_60s": f60, "flow_5m": f300,
            "walls": self.book.wall_events[-5:],
            "data_quality": {
                "book_age_s": round(age, 2) if age is not None else None,
                "stale": age is None or age > 5 or (source_div is not None and source_div > max_div),
                "feed_divergence_bps": round(div, 2) if div is not None else None,
                "feed_gaps": self.gaps,
                "checksum_ok": self.book.checksum_ok, "checksum_fail": self.book.checksum_fail,
                "exchanges_fresh": len(fresh_sources),
            },
        }


def minute_candles(analytics, now=None, history=None, n=300):
    """1-minute OHLCV candles for the features.

    Built from COINBASE trades (live "matches" stream) on top of Coinbase's own
    historical candles, so live features are computed from exactly the same
    kind of data the model was trained on (no train/serve skew). Falls back to
    Kraken mids only for minutes with no Coinbase data at all."""
    now = now or time.time()
    bars = {c["ts"]: dict(c) for c in (history or [])}
    live = {}
    for ts, p, q in analytics.cb_trades:
        m = int(ts) - int(ts) % 60
        b = live.get(m)
        if b is None:
            live[m] = {"ts": m, "open": p, "high": p, "low": p, "close": p, "volume": q}
        else:
            b["high"], b["low"] = max(b["high"], p), min(b["low"], p)
            b["close"] = p
            b["volume"] += q
    if not live:   # no Coinbase trades yet: price-only bars from Kraken mids
        for ts, mid in analytics.mids:
            m = int(ts) - int(ts) % 60
            if m in bars and "live" not in bars[m]:
                continue
            b = live.get(m)
            if b is None:
                live[m] = {"ts": m, "open": mid, "high": mid, "low": mid, "close": mid, "volume": 0.0}
            else:
                b["high"], b["low"], b["close"] = max(b["high"], mid), min(b["low"], mid), mid
    for m, b in live.items():
        b["live"] = True
        bars[m] = b
    return [bars[k] for k in sorted(bars)][-n:]
