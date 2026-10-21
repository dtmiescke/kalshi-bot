import os
import tempfile
import unittest

from bot.storage import Store
from bot.combo_research import _market_ask, combo_ranking_score, conservative_leg_probability, score_combo_probabilities


class TestComboResearch(unittest.TestCase):
    def test_combo_probability_is_discounted_for_joint_uncertainty(self):
        result = score_combo_probabilities([0.8, 0.75], 0.45, 0.80, fee_rate=0.0, min_edge=0.01)
        self.assertAlmostEqual(result["independence_product"], 0.6)
        self.assertAlmostEqual(result["uncertainty_haircut"], 0.03)
        self.assertAlmostEqual(result["candidates"][0]["conservative_win_probability"], 0.57)
        self.assertTrue(result["eligible"])

    def test_fee_can_turn_apparent_edge_into_rejection(self):
        no_fee = score_combo_probabilities([0.8, 0.8], 0.55, 0.45, fee_rate=0.0, min_edge=0.05)
        with_fee = score_combo_probabilities([0.8, 0.8], 0.55, 0.45, fee_rate=0.07, min_edge=0.05)
        self.assertTrue(no_fee["eligible"])
        self.assertFalse(with_fee["eligible"])
        self.assertGreater(with_fee["candidates"][0]["fee_per_contract"], 0)

    def test_no_side_uses_upper_joint_probability_bound(self):
        result = score_combo_probabilities([0.9, 0.9], 0.85, 0.15, fee_rate=0.0, min_edge=0.0)
        no = next(x for x in result["candidates"] if x["side"] == "no")
        self.assertAlmostEqual(no["conservative_win_probability"], 1 - result["independence_product"] - result["uncertainty_haircut"])
        self.assertLess(no["conservative_win_probability"], 0.2)

    def test_invalid_probabilities_are_not_scored(self):
        self.assertFalse(score_combo_probabilities([0.8], 0.2, 0.8)["eligible"])
        self.assertFalse(score_combo_probabilities([0.8, 1.0], 0.2, 0.8)["eligible"])

    def test_weak_or_single_signal_forecasts_shrink_toward_half(self):
        raw = 0.80
        weak = conservative_leg_probability(raw, 0.40, 1)
        strong = conservative_leg_probability(raw, 0.90, 3)
        self.assertLess(weak, strong)
        self.assertLess(weak, raw)
        self.assertGreater(weak, 0.50)
        self.assertAlmostEqual(conservative_leg_probability(0.80, 1.0, 4), 0.80)

    def test_invalid_probability_is_neutralized(self):
        self.assertEqual(conservative_leg_probability(1.2, 0.9, 3), 0.5)
        self.assertEqual(conservative_leg_probability(0.8, -2, 1), 0.5)

    def test_prefers_three_to_five_legs_unless_long_combo_is_exceptional(self):
        five = combo_ranking_score(5, 0.10, 0.70, 0.06)
        six_ordinary = combo_ranking_score(6, 0.11, 0.70, 0.06)
        six_exceptional = combo_ranking_score(6, 0.15, 0.90, 0.06)
        self.assertEqual(five["length_preference_multiplier"], 1.0)
        self.assertEqual(six_ordinary["length_preference_multiplier"], 0.85)
        self.assertFalse(six_ordinary["exceptional_long_combo"])
        self.assertEqual(six_exceptional["length_preference_multiplier"], 1.0)
        self.assertTrue(six_exceptional["exceptional_long_combo"])

    def test_combo_audit_upserts_one_market_and_grades_only_real_outcome(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "test.db"))
            row = {"ticker": "COMBO-1", "ts": 100, "event_ticker": "EVENT-1",
                   "leg_count": 3, "side": "yes", "market_price": 0.25,
                   "predicted_yes": 0.40, "predicted_side_prob": 0.37,
                   "market_yes_proxy": 0.30, "fee": 0.02,
                   "expected_profit": 0.10, "eligible": True,
                   "ranking_score": 0.10, "legs": [{"ticker": "LEG-1"}]}
            store.record_combo_prediction(row)
            self.assertEqual(store.q("SELECT COUNT(*) n FROM combo_prediction_audit")[0]["n"], 1)
            self.assertEqual(store.q("SELECT COUNT(*) n FROM combo_prediction_audit WHERE outcome IS NOT NULL")[0]["n"], 0)
            # Repeated scans update the same ticker rather than inflating sample size.
            store.record_combo_prediction({**row, "ts": 200, "predicted_yes": 0.45})
            self.assertEqual(store.q("SELECT COUNT(*) n FROM combo_prediction_audit")[0]["n"], 1)
            self.assertEqual(store.q("SELECT predicted_yes FROM combo_prediction_audit")[0]["predicted_yes"], 0.45)
            self.assertEqual(store.grade_combo_prediction("COMBO-1", 1, 300), 1)
            self.assertEqual(store.grade_combo_prediction("COMBO-1", 0, 400), 0)
            graded = store.q("SELECT * FROM combo_prediction_audit")[0]
            self.assertEqual(graded["outcome"], 1)
            self.assertAlmostEqual(graded["brier"], (0.45 - 1) ** 2)
            self.assertAlmostEqual(graded["market_proxy_brier"], (0.30 - 1) ** 2)
            self.assertAlmostEqual(graded["hypothetical_pnl"], 1 - 0.25 - 0.02)
            # A settled prediction is immutable, even if a stale scan sees it again.
            store.record_combo_prediction({**row, "ts": 500, "predicted_yes": 0.99})
            self.assertEqual(store.q("SELECT predicted_yes FROM combo_prediction_audit")[0]["predicted_yes"], 0.45)
            self.assertEqual(store.combo_prediction_summary()[0]["markets"], 1)

    def test_uses_real_asks_or_mirrors_opposite_bid(self):
        self.assertEqual(_market_ask({"yes_ask_dollars": "0.4200"}, "yes"), 0.42)
        self.assertEqual(_market_ask({"no_bid_dollars": "0.3000"}, "yes"), 0.7)
        self.assertEqual(_market_ask({"yes_bid_dollars": "0.2500"}, "no"), 0.75)
        self.assertIsNone(_market_ask({}, "yes"))


if __name__ == "__main__":
    unittest.main()
