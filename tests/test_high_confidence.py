import unittest

from bot.high_confidence import (
    evaluate_paper_bets,
    evaluate_predictions,
    format_report,
    report,
    wilson_interval,
)


class HighConfidenceEvaluationTests(unittest.TestCase):
    def test_wilson_interval_handles_empty_and_known_sample(self):
        self.assertEqual(wilson_interval(0, 0), (None, None))
        low, high = wilson_interval(90, 100)
        self.assertLess(low, 0.90)
        self.assertGreater(high, 0.90)

    def test_predictions_use_extreme_probability_and_skip_invalid_rows(self):
        rows = [
            {"model_prob": 0.95, "outcome": 1},
            {"model_prob": 0.04, "outcome": 0},
            {"model_prob": 0.80, "outcome": 1},
            {"model_prob": float("nan"), "outcome": 1},
            {"model_prob": 0.99, "outcome": 4},
        ]
        result = evaluate_predictions(rows, min_sample=2)
        self.assertEqual(result["all"]["n"], 3)
        self.assertEqual(result["high_confidence"]["n"], 2)
        self.assertEqual(result["high_confidence"]["wins"], 2)
        self.assertFalse(result["high_confidence"]["target_met"])

    def test_paper_bet_accuracy_uses_selected_side_not_yes_probability_alone(self):
        rows = [
            {"model_prob": 0.05, "side": "no", "result": "no", "pnl": 0.1},
            {"model_prob": 0.95, "side": "no", "result": "yes", "pnl": -0.9},
            {"model_prob": 0.95, "side": "yes", "result": "yes", "pnl": 0.1},
        ]
        result = evaluate_paper_bets(rows, min_sample=2)
        self.assertEqual(result["all"]["wins"], 2)
        self.assertEqual(result["high_confidence"]["n"], 3)
        self.assertEqual(result["high_confidence"]["wins"], 2)
        self.assertAlmostEqual(result["all"]["net_pnl"], -0.7)
        self.assertFalse(result["all"]["target_met"])

    def test_target_requires_minimum_sample_and_confidence_bound(self):
        rows = [{"model_prob": 0.99, "outcome": 1} for _ in range(99)]
        result = evaluate_predictions(rows, min_sample=100)
        self.assertEqual(result["high_confidence"]["n"], 99)
        self.assertFalse(result["high_confidence"]["target_met"])
        rows.append({"model_prob": 0.99, "outcome": 1})
        result = evaluate_predictions(rows, min_sample=100)
        self.assertFalse(result["high_confidence"]["target_met"])

    def test_report_text_does_not_claim_success_without_data(self):
        class EmptyStore:
            def q(self, sql, args=()):
                return []
        result = report(EmptyStore())
        text = format_report(result)
        self.assertIn("NOT PROVEN", text)
        self.assertIn("no graded samples yet", text)


if __name__ == "__main__":
    unittest.main()
