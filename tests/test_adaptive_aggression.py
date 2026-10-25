"""Regression tests for additive paper-only aggression."""
import unittest
from bot.adaptive_aggression import AggressionPolicy, choose
from bot.config import Settings


class TestAggression(unittest.TestCase):
    def test_requested_default(self):
        self.assertEqual(Settings().aggression_level, "aggressive")

    def test_off_is_inert(self):
        self.assertIsNone(choose(AggressionPolicy(level="off")))

    def test_aggressive_keeps_sizing_cap(self):
        policy = choose(AggressionPolicy(level="aggressive"))
        self.assertGreaterEqual(policy["min_edge"], 0.03)
        self.assertLess(policy["min_confidence"], 0.55)
        self.assertLessEqual(policy["kelly_fraction"], 0.25)

    def test_adaptive_needs_held_out_history(self):
        self.assertEqual(choose(AggressionPolicy(level="adaptive"))["effective_level"], "conservative")
        bad = [{"mode": "paper", "held_out": False, "settled": True, "pnl": 1}] * 50
        self.assertEqual(choose(AggressionPolicy(level="adaptive"), bad)["effective_level"], "conservative")

    def test_invalid_level_fails_closed(self):
        with self.assertRaises(ValueError):
            AggressionPolicy(level="reckless")


if __name__ == "__main__":
    unittest.main()
