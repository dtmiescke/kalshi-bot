import unittest

from bot.contextual_calibration import contextual_policy


class ContextualCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.base = {"min_edge": 0.03, "min_confidence": 0.55,
                     "kelly_fraction": 0.25, "effective_level": "aggressive"}

    def test_high_quality_depth_preserves_or_tightens(self):
        result = contextual_policy(self.base, model_prob=0.9, confidence=0.9,
                                   book={"yes": [(0.6, 100)], "no": [(0.4, 100)]})
        self.assertGreaterEqual(result["min_edge"], self.base["min_edge"])
        self.assertLessEqual(result["kelly_fraction"], self.base["kelly_fraction"])
        self.assertEqual(self.base["min_edge"], 0.03)

    def test_missing_book_is_conservative(self):
        result = contextual_policy(self.base, model_prob=0.6, confidence=0.6, book=None)
        self.assertGreater(result["min_edge"], self.base["min_edge"])
        self.assertGreater(result["min_confidence"], self.base["min_confidence"])
        self.assertLess(result["kelly_fraction"], self.base["kelly_fraction"])

    def test_malformed_forecast_fails_closed(self):
        result = contextual_policy(self.base, model_prob=float("nan"), confidence=0.9,
                                   book={"yes": [], "no": []})
        self.assertEqual(result["context_caution"], 1.0)

    def test_never_changes_none(self):
        self.assertIsNone(contextual_policy(None, model_prob=0.5, confidence=0.5, book={}))

    def test_no_increase_even_when_policy_already_conservative(self):
        conservative = {"min_edge": 0.05, "min_confidence": 0.7,
                        "kelly_fraction": 0.1}
        result = contextual_policy(conservative, model_prob=0.5, confidence=0.2,
                                   book={"yes": [], "no": []})
        self.assertGreaterEqual(result["min_edge"], conservative["min_edge"])
        self.assertGreaterEqual(result["min_confidence"], conservative["min_confidence"])
        self.assertLessEqual(result["kelly_fraction"], conservative["kelly_fraction"])


if __name__ == "__main__":
    unittest.main()
