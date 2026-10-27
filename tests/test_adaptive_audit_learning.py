import unittest
from bot.live.adaptive_learning import graded_setup_performance, historical_setup_penalty


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
    def q(self, sql, args=()):
        return self.rows


class AdaptiveAuditTests(unittest.TestCase):
    def test_setup_report_uses_only_graded_canonical_rows(self):
        rows = [
            {"model_prob": 0.8, "market_price": 0.5, "outcome": 1,
             "signals_json": '{"chart_setups":["range_breakout"],"regime":"trending"}',
             "settled_ts": 100},
            {"model_prob": 0.2, "market_price": 0.5, "outcome": 0,
             "signals_json": '{"chart_setups":["range_breakout"],"regime":"trending"}',
             "settled_ts": 101},
        ]
        report = graded_setup_performance(FakeStore(rows), min_samples=2)
        item = report["range_breakout@trending"]
        self.assertEqual(item["samples"], 2)
        self.assertTrue(item["beats_market_brier"])

    def test_penalty_only_activates_with_sufficient_bad_evidence(self):
        rows = [
            {"model_prob": 0.9, "market_price": 0.5, "outcome": 0,
             "signals_json": '{"chart_setups":["bad_setup"],"regime":"dislocated"}',
             "settled_ts": i}
            for i in range(25)
        ]
        report = graded_setup_performance(FakeStore(rows), min_samples=20)
        penalty = historical_setup_penalty(FakeStore([]), ["bad_setup"], "dislocated",
                                           min_samples=20, report=report)
        self.assertGreater(penalty["surcharge"], 0)
        self.assertLessEqual(penalty["surcharge"], 0.015)

    def test_no_history_does_not_penalize(self):
        penalty = historical_setup_penalty(FakeStore([]), ["unknown"], "mixed")
        self.assertEqual(penalty["surcharge"], 0.0)


if __name__ == "__main__":
    unittest.main()
