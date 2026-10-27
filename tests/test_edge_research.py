import unittest

from bot.live.edge_research import (
    classify_regime, detect_feature_drift, evidence_score, graded_feature_drift, walk_forward_summary,
)


class EdgeResearchTests(unittest.TestCase):
    def test_regime_exposes_multiple_risks(self):
        tags = classify_regime({
            "source_divergence_bps": 40, "spread_bps": 300,
            "volatility_ratio": 3, "trend_strength": 0.8, "book_imbalance": 0.7,
        })
        self.assertIn("feed_disagreement", tags)
        self.assertIn("wide_kalshi_spread", tags)
        self.assertIn("volatility_shock", tags)
        self.assertIn("trending", tags)

    def test_walk_forward_is_chronological_and_cost_aware(self):
        trades = [{"entry_ts": i, "net_return": 0.01 if i % 3 else -0.006} for i in range(100)]
        result = walk_forward_summary(trades, min_train=30, min_test=20, cost_per_trade=0.002)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["train_samples"], 70)
        self.assertEqual(result["test_samples"], 30)
        self.assertLess(result["test_mean_net_return"], 0.01)

    def test_tiny_sample_never_qualifies(self):
        result = walk_forward_summary([{"entry_ts": i, "net_return": 0.01} for i in range(10)])
        self.assertFalse(result["eligible"])

    def test_evidence_requires_oos_and_cost_coverage(self):
        weak = evidence_score(samples=200, out_of_sample_samples=0, net_edge=0.1)
        stronger = evidence_score(samples=200, out_of_sample_samples=100, net_edge=0.1,
                                  profit_factor=1.4, max_drawdown=0.05, brier=0.1,
                                  baseline_brier=0.2, cost_coverage=True)
        self.assertGreater(stronger, weak)


    def test_persisted_drift_report_uses_chronological_settled_rows(self):
        import json
        rows = []
        for i in range(100):
            value = float(i % 3) if i < 70 else 20.0 + float(i % 3)
            rows.append({"signals_json": json.dumps({"indicators": {"rsi_1m": value}}),
                         "settled_ts": i})
        class Store:
            def q(self, sql, args=()):
                # Emulate SQL ORDER BY settled_ts DESC LIMIT ?.
                return sorted(rows, key=lambda row: row["settled_ts"], reverse=True)[:args[0]]
        report = graded_feature_drift(Store(), min_reference=50, min_recent=20)
        self.assertIn("rsi_1m", report["features"])
        self.assertIn("rsi_1m", report["drift_alerts"])

    def test_drift_detected_after_distribution_shift(self):
        ref = [float(i % 3) for i in range(100)]
        recent = [20.0 + float(i % 3) for i in range(30)]
        result = detect_feature_drift(ref, recent)
        self.assertTrue(result["drift_detected"])


if __name__ == "__main__":
    unittest.main()
