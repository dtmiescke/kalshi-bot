"""Readings anchored to the Kalshi price (trade flow, the Kalshi order-book
nudge) measure the edge; they must not count as outside signals disagreeing
with the model, or confidence drops exactly when the model sees an edge."""
import unittest

from bot import brain
from bot.signals.base import Reading


class TestMarketAnchoredReadings(unittest.TestCase):
    def test_kalshi_orderbook_nudge_does_not_lower_confidence(self):
        base = [Reading("kalshi_market", 0.60, 0.95), Reading("crypto_model", 0.90, 0.75),
                Reading("crypto_model_iv", 0.88, 0.6)]
        _, conf = brain.blend(base, brain.DEFAULT_WEIGHTS)
        _, with_book = brain.blend(base + [Reading("crypto_kalshi_orderbook", 0.605, 0.25)],
                                   brain.DEFAULT_WEIGHTS)
        self.assertGreaterEqual(with_book, conf * 0.95)

    def test_real_outside_disagreement_still_lowers_confidence(self):
        base = [Reading("kalshi_market", 0.60, 0.95), Reading("crypto_model", 0.90, 0.75)]
        _, conf = brain.blend(base + [Reading("crypto_model_iv", 0.88, 0.6)], brain.DEFAULT_WEIGHTS)
        _, split = brain.blend(base + [Reading("crypto_model_iv", 0.20, 0.6)], brain.DEFAULT_WEIGHTS)
        self.assertLess(split, conf)


if __name__ == "__main__":
    unittest.main()
