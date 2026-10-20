"""Live Level-2 order book for a spot exchange (Kraken), kept current from
snapshot + incremental updates, plus the liquidity measurements the council uses.

Remember: this is VISIBLE resting liquidity on ONE exchange. Walls can be
pulled at any moment, so walls are always checked against what actually trades.
"""
import time
import zlib

# fallback decimal precisions (the live "instrument" channel overrides these)
DEFAULT_PRECISION = {"BTC/USD": (1, 8), "ETH/USD": (2, 8), "SOL/USD": (2, 8)}


class ChecksumError(Exception):
    """Our copy of the book no longer matches Kraken's: reconnect for a snapshot."""


def _fmt(x, decimals):
    return f"{float(x):.{decimals}f}".replace(".", "").lstrip("0") or "0"


class OrderBook:
    def __init__(self, symbol, depth=100):
        self.symbol = symbol
        self.depth = depth
        self.bids = {}            # price -> qty
        self.asks = {}
        self.last_update = 0.0
        self.updates = 0
        self.walls = {}           # (side, price) -> {"qty", "first_seen"}
        self.precision = DEFAULT_PRECISION.get(symbol)   # (price, qty) decimals
        self.checksum_ok = 0
        self.checksum_fail = 0
        self.wall_events = []     # recent appear / pulled / consumed events

    # --- maintenance -------------------------------------------------------------
    def snapshot(self, bids, asks, ts=None):
        self.bids = {float(p): float(q) for p, q in bids if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in asks if float(q) > 0}
        self._touch(ts)

    def update(self, side, price, qty, ts=None):
        book = self.bids if side == "bid" else self.asks
        price, qty = float(price), float(qty)
        if qty <= 0:
            book.pop(price, None)
        else:
            book[price] = qty
        self._trim()
        self._touch(ts)

    def _trim(self):
        if len(self.bids) > self.depth:
            for p in sorted(self.bids)[: len(self.bids) - self.depth]:
                del self.bids[p]
        if len(self.asks) > self.depth:
            for p in sorted(self.asks, reverse=True)[: len(self.asks) - self.depth]:
                del self.asks[p]

    def _touch(self, ts):
        self.last_update = ts or time.time()
        self.updates += 1

    def checksum(self):
        """Kraken v2 CRC32 over the top 10 asks (low to high) then top 10 bids
        (high to low), each level as price+qty digits without the decimal
        point or leading zeros."""
        pp, qp = self.precision
        s = ""
        for p in sorted(self.asks)[:10]:
            s += _fmt(p, pp) + _fmt(self.asks[p], qp)
        for p in sorted(self.bids, reverse=True)[:10]:
            s += _fmt(p, pp) + _fmt(self.bids[p], qp)
        return zlib.crc32(s.encode()) & 0xFFFFFFFF

    def verify(self, expected):
        if expected is None or not self.precision:
            return True
        if self.checksum() == int(expected):
            self.checksum_ok += 1
            return True
        self.checksum_fail += 1
        self.last_update = 0.0          # stale until a fresh snapshot arrives
        raise ChecksumError(f"{self.symbol} checksum mismatch")

    # --- measurements ------------------------------------------------------------
    def best(self):
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        return bb, ba

    def mid(self):
        bb, ba = self.best()
        return (bb + ba) / 2 if bb and ba else None

    def spread_bps(self):
        bb, ba = self.best()
        m = self.mid()
        return (ba - bb) / m * 1e4 if m else None

    def depth_within(self, pct):
        """Quote-currency depth (e.g. USD) within pct% of mid, each side."""
        m = self.mid()
        if not m:
            return 0.0, 0.0
        lo, hi = m * (1 - pct / 100), m * (1 + pct / 100)
        bid = sum(p * q for p, q in self.bids.items() if p >= lo)
        ask = sum(p * q for p, q in self.asks.items() if p <= hi)
        return bid, ask

    def imbalance(self, pct=0.1):
        """+1 = all resting size on the bid, -1 = all on the ask."""
        b, a = self.depth_within(pct)
        return (b - a) / (b + a) if (b + a) else 0.0

    def track_walls(self, recent_trades, multiple=5.0, now=None, keep=50):
        """Find levels much bigger than typical, and note when they appear,
        get pulled (vanish without trading) or get consumed (traded through)."""
        now = now or time.time()
        sizes = sorted(list(self.bids.values()) + list(self.asks.values()))
        if len(sizes) < 10:
            return []
        median = sizes[len(sizes) // 2]
        current = {}
        for side, book in (("bid", self.bids), ("ask", self.asks)):
            for p, q in book.items():
                if q >= multiple * median:
                    current[(side, p)] = q
        events = []
        for key, q in current.items():
            if key not in self.walls:
                self.walls[key] = {"qty": q, "first_seen": now}
                events.append({"ts": now, "event": "appeared", "side": key[0], "price": key[1], "qty": q})
        for key in list(self.walls):
            if key in current:
                self.walls[key]["qty"] = current[key]
                continue
            side, p = key
            traded = any(abs(t["price"] - p) / p < 1e-4 for t in recent_trades)
            events.append({"ts": now, "event": "consumed" if traded else "pulled",
                           "side": side, "price": p, "qty": self.walls[key]["qty"],
                           "lived_s": round(now - self.walls[key]["first_seen"], 1)})
            del self.walls[key]
        self.wall_events = (self.wall_events + events)[-keep:]
        return events
