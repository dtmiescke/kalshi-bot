import unittest

from bot.combo_research import _market_ask, combo_ranking_score, score_combo_probabilities


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

    def test_prefers_three_to_five_legs_unless_long_combo_is_exceptional(self):
        five = combo_ranking_score(5, 0.10, 0.70, 0.06)
        six_ordinary = combo_ranking_score(6, 0.11, 0.70, 0.06)
        six_exceptional = combo_ranking_score(6, 0.15, 0.90, 0.06)
        self.assertEqual(five["length_preference_multiplier"], 1.0)
        self.assertEqual(six_ordinary["length_preference_multiplier"], 0.85)
        self.assertFalse(six_ordinary["exceptional_long_combo"])
        self.assertEqual(six_exceptional["length_preference_multiplier"], 1.0)
        self.assertTrue(six_exceptional["exceptional_long_combo"])

    def test_uses_real_asks_or_mirrors_opposite_bid(self):
        self.assertEqual(_market_ask({"yes_ask_dollars": "0.4200"}, "yes"), 0.42)
        self.assertEqual(_market_ask({"no_bid_dollars": "0.3000"}, "yes"), 0.7)
        self.assertEqual(_market_ask({"yes_bid_dollars": "0.2500"}, "no"), 0.75)
        self.assertIsNone(_market_ask({}, "yes"))


if __name__ == "__main__":
    unittest.main()
