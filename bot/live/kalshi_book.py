"""Kalshi contract order book kept live from WebSocket snapshot + deltas.

Kalshi books hold BIDS only: a YES ask is the mirror of a NO bid
(buying YES at $0.62 = someone bidding NO at $0.38).
"""
import time


def _px(p):
    p = float(p)
    return p / 100.0 if p > 1.0 else p


class KalshiBook:
    def __init__(self, ticker):
        self.ticker = ticker
        self.yes = {}     # yes bid price -> qty
        self.no = {}      # no bid price -> qty
        self.seq = None
        self.last_update = 0.0
        self.gaps = 0
        self.needs_resync = False

    @staticmethod
    def _levels(msg, side):
        # Kalshi WS docs (websockets/orderbook-updates): snapshots carry
        # yes_dollars_fp / no_dollars_fp as [price_dollars, count_fp] pairs, and
        # an empty side is simply absent. Older shapes are kept as fallbacks.
        return (msg.get(f"{side}_dollars_fp") or msg.get(f"{side}_dollars")
                or msg.get(side) or [])

    def snapshot(self, msg, seq=None):
        self.yes = {_px(p): float(q) for p, q in self._levels(msg, "yes")}
        self.no = {_px(p): float(q) for p, q in self._levels(msg, "no")}
        self.seq = seq
        self.needs_resync = False
        self.last_update = time.time()

    def delta(self, msg, seq=None):
        if seq is not None and self.seq is not None and seq != self.seq + 1:
            # missed an update: the book is now WRONG. Mark it stale (no trading
            # on it) and ask for a fresh snapshot instead of trading on a ghost book.
            self.gaps += 1
            self.needs_resync = True
            self.last_update = 0.0
            self.seq = seq
            return
        self.seq = seq if seq is not None else self.seq
        side = self.yes if msg.get("side") == "yes" else self.no
        price = _px(msg.get("price_dollars") or msg.get("price"))
        qty = side.get(price, 0.0) + float(msg.get("delta_fp") or msg.get("delta") or 0)
        if qty <= 0:
            side.pop(price, None)
        else:
            side[price] = qty
        self.last_update = time.time()

    def as_rest_book(self):
        """Same shape the REST client returns, so risk.py works unchanged."""
        return {"yes": sorted(self.yes.items(), key=lambda x: -x[0]),
                "no": sorted(self.no.items(), key=lambda x: -x[0])}

    def mid(self):
        if not self.yes or not self.no:
            return None
        best_yes_bid = max(self.yes)
        best_yes_ask = 1 - max(self.no)
        return (best_yes_bid + best_yes_ask) / 2

    def age(self):
        return None if (self.needs_resync or not self.last_update) else time.time() - self.last_update
