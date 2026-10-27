"""Unit tests for conservative edge quality and fee-aware EV diagnostics."""
import unittest
from types import SimpleNamespace

from bot.live.edge_quality import edge_quality, enrich_plan


class EdgeQualityTests(unittest.TestCase):
    def test_disagreement_and_missing_sources_raise_required_margin(self):
        readings = [
            SimpleNamespace(name="crypto_model", prob=0.60),
            SimpleNamespace(name="ml_model", prob=0.52),
            SimpleNamespace(name="kalshi_market", prob=0.50),
        ]
        q = edge_quality(readings, {"source_count": 1, "spread_bps": None})
        self.assertGreater(q["min_edge_surcharge"], 0)
        self.assertEqual(q["source_count"], 1)
        self.assertEqual(q["signal_count"], 2)

    def test_agreement_and_multiple_sources_have_lower_penalty(self):
        readings = [
            SimpleNamespace(name="crypto_model", prob=0.60),
            SimpleNamespace(name="ml_model", prob=0.60),
            SimpleNamespace(name="kalshi_market", prob=0.10),
        ]
        good = edge_quality(readings, {"source_count": 3, "spread_bps": 1.0})
        weak = edge_quality(readings, {"source_count": 1, "spread_bps": None})
        self.assertLess(good["min_edge_surcharge"], weak["min_edge_surcharge"])
        self.assertEqual(good["signal_count"], 2)

    def test_surcharge_is_capped(self):
        readings = [
            SimpleNamespace(name="crypto_model", prob=0.01),
            SimpleNamespace(name="ml_model", prob=0.99),
        ]
        q = edge_quality(readings, {"source_count": 2, "spread_bps": 1000})
        self.assertLessEqual(q["min_edge_surcharge"], 0.015)

    def test_plan_reports_ev_after_fees(self):
        plan = {"contracts": 10, "edge": 0.05, "stake": 5.0, "fee": 0.10,
                "price": 0.50, "side": "yes"}
        result = enrich_plan(plan, {"min_edge_surcharge": 0.01}, 0.6)
        self.assertEqual(result["expected_value_after_fees"], 0.5)
        self.assertEqual(result["total_cost_including_fees"], 5.1)
        self.assertEqual(result["break_even_probability"], 0.51)
        self.assertEqual(result["model_probability_for_side"], 0.6)


if __name__ == "__main__":
    unittest.main()
