"""Regression tests for crypto order-book learning and signal contribution."""
import unittest

from bot import brain
from bot.signals.base import Reading


class MemoryStore:
    def __init__(self):
        self.weights = {}
        self.history = []

    def current_weights(self):
        return dict(self.weights)

    def set_weight(self, key, value, reason=""):
        self.weights[key] = value
        self.history.append((key, value, reason))


class CryptoEdgeLearningTests(unittest.TestCase):
    def test_orderbook_signal_has_explicit_learnable_starting_weight(self):
        self.assertIn("crypto_orderbook", brain.DEFAULT_WEIGHTS)
        self.assertGreater(brain.DEFAULT_WEIGHTS["crypto_orderbook"], 0)

    def test_orderbook_weight_increases_when_it_beats_market(self):
        store = MemoryStore()
        rows = []
        for i in range(20):
            y = i % 2
            rows.append({
                "kalshi_market": 0.55 if y else 0.45,
                "crypto_orderbook": 0.8 if y else 0.2,
                "_outcome": y,
            })
        changes = brain.learn(store, rows, "crypto")
        self.assertIn("crypto_orderbook", changes)
        self.assertGreater(changes["crypto_orderbook"][1], changes["crypto_orderbook"][0])
        self.assertIn("crypto_orderbook@crypto", store.weights)

    def test_orderbook_weight_decreases_when_it_loses_to_market(self):
        store = MemoryStore()
        rows = []
        for i in range(20):
            y = i % 2
            rows.append({
                "kalshi_market": 0.8 if y else 0.2,
                "crypto_orderbook": 0.55 if y else 0.45,
                "_outcome": y,
            })
        changes = brain.learn(store, rows, "crypto")
        self.assertIn("crypto_orderbook", changes)
        self.assertLess(changes["crypto_orderbook"][1], changes["crypto_orderbook"][0])

    def test_orderbook_signal_participates_in_probability_blend(self):
        readings = [
            Reading("kalshi_market", 0.5, 1.0, {}),
            Reading("crypto_orderbook", 0.7, 0.4, {}),
        ]
        prob, confidence = brain.blend(readings, brain.get_weights(MemoryStore(), "crypto"))
        self.assertGreater(prob, 0.5)
        self.assertGreater(confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
