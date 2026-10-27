import unittest
from bot.live.adaptive_learning import AdaptiveLearning

class AdaptiveLearningTests(unittest.TestCase):
    def test_only_resolved_predictions_update_learning(self):
        learner = AdaptiveLearning(min_samples=10)
        learner.record_prediction("p1", "breakout", "trending", "yes", .7, .5, .55)
        self.assertEqual(learner.report("breakout")["samples"], 0)
        result = learner.resolve("p1", True)
        self.assertTrue(result["correct"])
        self.assertEqual(learner.report("breakout", "trending")["samples"], 1)

    def test_duplicate_prediction_is_rejected(self):
        learner = AdaptiveLearning()
        learner.record_prediction("p1", "meanrev", "ranging", "no", .4, .5, .52)
        with self.assertRaises(ValueError):
            learner.record_prediction("p1", "meanrev", "ranging", "no", .4, .5, .52)

    def test_no_unseen_strategy_gets_weight(self):
        learner = AdaptiveLearning(min_samples=10)
        self.assertEqual(learner.candidate_weights(["new_strategy"], "mixed"), {})

    def test_dislocation_regime(self):
        self.assertEqual(AdaptiveLearning.regime({"spread_bps": 45}), "dislocated")

if __name__ == "__main__":
    unittest.main()
