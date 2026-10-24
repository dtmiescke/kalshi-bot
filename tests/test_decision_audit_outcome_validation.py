"""Reject invalid settlement labels before they enter model evaluation."""
import os
import tempfile
import unittest

from bot.storage import Store


class TestDecisionAuditOutcomeValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_nonbinary_outcome_is_rejected_without_grading_rows(self):
        self.store.add_decision_audit({
            "ts": 100, "ticker": "MARKET-1", "decision": "candidate",
            "side": "yes", "market_price": 0.4, "model_prob": 0.7,
        })
        for outcome in (None, -1, 2, "yes", float("nan"), float("inf")):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(ValueError, "must be binary"):
                    self.store.grade_decision_audit("MARKET-1", outcome, settled_ts=200)
                row = self.store.q(
                    "SELECT outcome, brier, settled_ts FROM decision_audit WHERE ticker=?",
                    ("MARKET-1",),
                )[0]
                self.assertIsNone(row["outcome"])
                self.assertIsNone(row["brier"])
                self.assertIsNone(row["settled_ts"])

    def test_binary_outcomes_are_accepted_and_scored(self):
        self.store.add_decision_audit({
            "ts": 100, "ticker": "MARKET-YES", "decision": "candidate",
            "side": "yes", "market_price": 0.4, "model_prob": 0.7,
        })
        self.assertEqual(
            self.store.grade_decision_audit("MARKET-YES", 1, settled_ts=200), 1
        )
        row = self.store.q(
            "SELECT outcome, brier, settled_ts FROM decision_audit WHERE ticker=?",
            ("MARKET-YES",),
        )[0]
        self.assertEqual(row["outcome"], 1)
        self.assertAlmostEqual(row["brier"], 0.09)
        self.assertEqual(row["settled_ts"], 200)


if __name__ == "__main__":
    unittest.main()
