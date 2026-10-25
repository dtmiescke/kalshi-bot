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

    def test_adaptive_uses_real_settled_paper_results(self):
        self.assertEqual(choose(AggressionPolicy(level="adaptive"))["effective_level"], "conservative")
        bad = [{"mode": "demo", "settled": True, "pnl": 1}] * 50
        self.assertEqual(choose(AggressionPolicy(level="adaptive"), bad)["effective_level"], "conservative")
        good = [{"mode": "paper", "settled": True, "pnl": 1} for _ in range(50)]
        self.assertEqual(choose(AggressionPolicy(level="adaptive"), good)["effective_level"], "aggressive")
        losses = [{"mode": "paper", "settled": True, "pnl": -1} for _ in range(50)]
        self.assertEqual(choose(AggressionPolicy(level="adaptive"), losses)["effective_level"], "conservative")

    def test_settled_paper_history_reads_existing_ledger(self):
        from bot.adaptive_aggression import settled_paper_results

        class FakeStore:
            def q(self, sql, args):
                self.sql, self.args = sql, args
                return [
                    {"mode": "paper", "pnl": 2.0, "graded_ts": 100, "result": "yes"},
                    {"mode": "paper", "pnl": -1.0, "graded_ts": 200, "result": "no"},
                ]

        store = FakeStore()
        rows = settled_paper_results(store)
        self.assertEqual([r["pnl"] for r in rows], [2.0, -1.0])
        self.assertIn("status='graded'", store.sql)
        self.assertIn("mode='paper'", store.sql)

    def test_unavailable_history_fails_closed_without_crashing(self):
        from bot.adaptive_aggression import safe_settled_paper_results

        class BrokenStore:
            def q(self, sql, args):
                raise RuntimeError("ledger unavailable")

        history = safe_settled_paper_results(BrokenStore())
        self.assertEqual(history, [])
        policy = choose(AggressionPolicy(level="adaptive"),
                        settled_paper_results=history)
        self.assertEqual(policy["effective_level"], "conservative")

    def test_invalid_level_fails_closed(self):
        with self.assertRaises(ValueError):
            AggressionPolicy(level="reckless")


if __name__ == "__main__":
    unittest.main()
