"""Corrupt historical labels must not enter model fitting or evaluation."""
import json
import unittest
from unittest.mock import patch

from bot import features, ml


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def x(self, *_args, **_kwargs):
        return None

    def q(self, *_args, **_kwargs):
        return self.rows


class TestMLDatasetLabelSafety(unittest.TestCase):
    def test_dataset_skips_nonbinary_and_non_integer_legacy_labels(self):
        feature_payload = json.dumps({"kalshi_logit": 0.25})
        labels = [1, 0, -1, 2, None, "1", 1.0, True]
        rows = [
            {"ticker": f"T{i}", "ts": i, "coin": "BTC",
             "features": feature_payload, "y": label}
            for i, label in enumerate(labels)
        ]
        store = FakeStore(rows)
        with patch.object(ml, "ensure_table", lambda _store: None):
            X, y, groups, ts, cols = ml.load_dataset(store)
        self.assertEqual(y.tolist(), [1, 0])
        self.assertEqual(groups.tolist(), ["T0", "T1"])
        self.assertEqual(ts.tolist(), [0, 1])
        self.assertEqual(X.shape[0], 2)
        self.assertEqual(X.shape[1], len(cols))


if __name__ == "__main__":
    unittest.main()
