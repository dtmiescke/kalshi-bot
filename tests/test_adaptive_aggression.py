"""Regression tests for bounded paper-only aggression profiles."""
import unittest
from bot.adaptive_aggression import AggressionPolicy, choose
from bot.config import Settings


class TestAggression(unittest.TestCase):
    def test_requested_default(self):
        self.assertEqual(Settings().aggression_level, "aggressive")

    def test_off_is_inert(self):
        self.assertIsNone(choose(AggressionPolicy(level="off")))

    def test_aggressive_preserves_edge_floor_and_size(self):
        policy = choose(AggressionPolicy(level="aggressive"))
        self.assertEqual(policy["min_edge"], 0.03)
        self.assertLess(policy["min_confidence"], 0.55)
        self.assertEqual(policy["kelly_fraction"], 0.25)

    def test_conservative_is_stricter_and_smaller(self):
        policy = choose(AggressionPolicy(level="conservative"))
        self.assertGreaterEqual(policy["min_edge"], 0.03)
        self.assertGreater(policy["min_confidence"], 0.55)
        self.assertLess(policy["kelly_fraction"], 0.25)

    def test_balanced_keeps_baseline_formula(self):
        policy = choose(AggressionPolicy(level="balanced"))
        self.assertEqual(policy["min_edge"], 0.03)
        self.assertEqual(policy["min_confidence"], 0.55)
        self.assertEqual(policy["kelly_fraction"], 0.25)

    def test_adaptive_needs_verified_held_out_history(self):
        self.assertEqual(choose(AggressionPolicy(level="adaptive"))["effective_level"], "conservative")
        bad = [{"mode": "paper", "held_out": False, "settled": True, "pnl": 1}] * 50
        self.assertEqual(choose(AggressionPolicy(level="adaptive"), bad)["effective_level"], "conservative")
        good = [{"mode": "paper", "held_out": True, "settled": True, "pnl": 1}] * 50
        self.assertEqual(choose(AggressionPolicy(level="adaptive"), good)["effective_level"], "aggressive")

    def test_invalid_level_fails_closed(self):
        with self.assertRaises(ValueError):
            AggressionPolicy(level="reckless")


if __name__ == "__main__":
    unittest.main()
