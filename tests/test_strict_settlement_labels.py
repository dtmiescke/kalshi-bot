"""Settlement labels must be genuine binary integers before scoring/training."""
import os
import tempfile
import unittest

from bot import ml
from bot.storage import Store


class TestStrictSettlementLabels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "labels.db"))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_ml_label_rejects_invalid_values_without_writing(self):
        ml.add_row(self.store, "KXBTC15M-1", 100, "BTC", {"kalshi_logit": 0.0})
        for bad in (None, -1, 2, True, 1.0, "1", float("nan")):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(ValueError, "integer 0 or 1"):
                    ml.label(self.store, "KXBTC15M-1", bad)
                row = self.store.q(
                    "SELECT y FROM ml_rows WHERE ticker=?", ("KXBTC15M-1",)
                )[0]
                self.assertIsNone(row["y"])

    def test_ml_label_accepts_binary_integer(self):
        ml.add_row(self.store, "KXBTC15M-2", 200, "BTC", {"kalshi_logit": 0.0})
        ml.label(self.store, "KXBTC15M-2", 1)
        self.assertEqual(
            self.store.q("SELECT y FROM ml_rows WHERE ticker=?", ("KXBTC15M-2",))[0]["y"], 1
        )

    def test_decision_audit_rejects_bool_and_float_outcomes(self):
        self.store.add_decision_audit({
            "ts": 100, "ticker": "AUDIT-1", "decision": "candidate",
            "side": "yes", "market_price": 0.4, "model_prob": 0.7,
        })
        for bad in (True, False, 1.0, 0.0, "1"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(ValueError, "integer 0 or 1"):
                    self.store.grade_decision_audit("AUDIT-1", bad, settled_ts=200)
        self.assertIsNone(self.store.q(
            "SELECT outcome FROM decision_audit WHERE ticker=?", ("AUDIT-1",)
        )[0]["outcome"])

    def test_combo_audit_ignores_invalid_outcomes(self):
        self.store.record_combo_prediction({
            "ticker": "COMBO-1", "ts": 100, "leg_count": 3,
            "side": "yes", "market_price": 0.4, "predicted_yes": 0.7,
            "eligible": True, "legs": [],
        })
        for bad in (True, False, 1.0, 0.0, "1", None):
            with self.subTest(value=bad):
                self.assertEqual(self.store.grade_combo_prediction("COMBO-1", bad, settled_ts=200), 0)
        self.assertIsNone(self.store.q(
            "SELECT outcome FROM combo_prediction_audit WHERE ticker=?", ("COMBO-1",)
        )[0]["outcome"])
        self.assertEqual(self.store.grade_combo_prediction("COMBO-1", 1, settled_ts=200), 1)


if __name__ == "__main__":
    unittest.main()
