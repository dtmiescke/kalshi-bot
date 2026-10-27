"""Tests for the 90% forecast and paper-bet scorecards."""
import unittest

from bot.backtest import score_high_confidence_predictions, _paper_crypto_bet_score


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def q(self, sql, params=()):
        return self.rows


class NinetyPercentAuditTests(unittest.TestCase):
    def test_target_requires_minimum_independent_sample(self):
        rows = [{"p": 0.95, "y": 1} for _ in range(99)]
        result = score_high_confidence_predictions(rows)
        self.assertEqual(result["accuracy"], 1.0)
        self.assertFalse(result["validated"])
        self.assertEqual(result["n"], 99)

    def test_target_passes_only_at_90_percent_with_minimum_sample(self):
        rows = [{"p": 0.95, "y": 1} for _ in range(90)]
        rows += [{"p": 0.05, "y": 0} for _ in range(10)]
        result = score_high_confidence_predictions(rows)
        self.assertEqual(result["n"], 100)
        self.assertEqual(result["accuracy"], 1.0)
        self.assertTrue(result["validated"])

    def test_low_confidence_and_invalid_forecasts_are_not_in_target_cohort(self):
        rows = [
            {"p": 0.60, "y": 1},
            {"p": 0.95, "y": 0},
            {"p": float("nan"), "y": 1},
            {"p": 0.95, "y": 2},
        ]
        result = score_high_confidence_predictions(rows)
        self.assertEqual(result["n"], 1)
        self.assertEqual(result["accuracy"], 0.0)
        self.assertFalse(result["validated"])

    def test_paper_bets_scored_separately_from_predictions(self):
        rows = [
            {"side": "yes", "result": "yes", "pnl": 0.25, "fee": 0.02},
            {"side": "no", "result": "yes", "pnl": -0.50, "fee": 0.02},
        ]
        result = _paper_crypto_bet_score(FakeStore(rows))
        self.assertEqual(result["bets"], 2)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["losses"], 1)
        self.assertEqual(result["win_rate"], 0.5)
        self.assertEqual(result["net_pnl"], -0.25)
        self.assertFalse(result["validated"])

    def test_no_paper_bets_is_not_a_pass(self):
        result = _paper_crypto_bet_score(FakeStore([]))
        self.assertEqual(result["bets"], 0)
        self.assertFalse(result["validated"])


if __name__ == "__main__":
    unittest.main()
