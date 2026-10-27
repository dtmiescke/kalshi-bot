import unittest

from bot.live.adaptive_learning import graded_price_discovery


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def q(self, sql, args=()):
        return self.rows


class PriceDiscoveryLearningTests(unittest.TestCase):
    def test_aggregates_only_valid_settled_snapshot_evidence(self):
        rows = [
            {"signals_json": '{"price_discovery":{"sources":{"kraken":{"median_follow_lag_s":1.2,"same_direction_rate":0.7,"observations":99}}}}'},
            {"signals_json": '{"price_discovery":{"sources":{"kraken":{"median_follow_lag_s":0.8,"same_direction_rate":0.9}}}}'},
            {"signals_json": '{"price_discovery":{"sources":{"coinbase":{"median_follow_lag_s":null,"same_direction_rate":0.5}}}}'},
        ]
        report = graded_price_discovery(FakeStore(rows), min_samples=2)
        self.assertEqual(report["kraken"]["graded_snapshots"], 2)
        self.assertAlmostEqual(report["kraken"]["mean_same_direction_rate"], 0.8)
        self.assertTrue(report["kraken"]["eligible_for_review"])
        self.assertNotIn("coinbase", report)

    def test_insufficient_sample_is_not_eligible(self):
        rows = [
            {"signals_json": '{"price_discovery":{"sources":{"kraken":{"median_follow_lag_s":2,"same_direction_rate":0.5}}}}'}
        ]
        report = graded_price_discovery(FakeStore(rows), min_samples=20)
        self.assertFalse(report["kraken"]["eligible_for_review"])


if __name__ == "__main__":
    unittest.main()
