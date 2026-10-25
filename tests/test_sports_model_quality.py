import unittest

from bot.sports_model_quality import (
    OPTIONAL_SPORTS_SOURCES,
    brier_score,
    calibration_bins,
    calibration_error,
    closing_line_value,
    log_loss,
    summarize_predictions,
)


class SportsModelQualityTests(unittest.TestCase):
    def test_perfect_predictions_score_better_than_wrong_predictions(self):
        perfect = [(0.9, 1), (0.1, 0)]
        wrong = [(0.1, 1), (0.9, 0)]
        self.assertLess(brier_score(perfect), brier_score(wrong))
        self.assertLess(log_loss(perfect), log_loss(wrong))

    def test_invalid_rows_are_ignored_and_empty_results_are_none(self):
        self.assertIsNone(brier_score([(float("nan"), 1), (0.7, 0.5)]))
        self.assertIsNone(log_loss([]))
        self.assertEqual(brier_score([(0.8, 1), (float("inf"), 0)]), 0.04)

    def test_calibration_bins_keep_counts_and_observed_rates(self):
        rows = calibration_bins([(0.11, 0), (0.19, 0), (0.91, 1)], bins=10)
        self.assertEqual([row["count"] for row in rows], [2, 1])
        self.assertAlmostEqual(rows[0]["mean_probability"], 0.15)
        self.assertEqual(rows[0]["observed_rate"], 0.0)
        self.assertAlmostEqual(calibration_error([(0.11, 0), (0.19, 0)], bins=10), 0.15)

    def test_clv_is_positive_when_entry_probability_is_below_close(self):
        self.assertAlmostEqual(closing_line_value(0.45, 0.52), 0.07)
        self.assertAlmostEqual(closing_line_value(0.52, 0.45), -0.07)
        self.assertIsNone(closing_line_value(0, 0.5))

    def test_summary_requires_minimum_sample_and_never_auto_promotes(self):
        rows = [{"probability": 0.8, "outcome": 1}] * 4
        report = summarize_predictions(rows, min_samples=5)
        self.assertEqual(report["sample_count"], 4)
        self.assertFalse(report["evaluation_ready"])
        self.assertFalse(report["promotion_allowed"])
        report = summarize_predictions(rows + [{"probability": 0.2, "outcome": 0}], min_samples=5)
        self.assertTrue(report["evaluation_ready"])
        self.assertFalse(report["promotion_allowed"])

    def test_source_catalog_is_advisory_and_does_not_enable_network_calls(self):
        self.assertIn("nflverse", OPTIONAL_SPORTS_SOURCES)
        self.assertEqual(OPTIONAL_SPORTS_SOURCES["nflverse"]["mode"],
                         "opt-in historical adapter required")


if __name__ == "__main__":
    unittest.main()
