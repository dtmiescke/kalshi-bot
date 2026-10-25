"""The crypto settlement model reuses the shared market-rule audit."""
import unittest

from bot.live.settlement import contract_terms


class TestSettlementRuleAuditIntegration(unittest.TestCase):
    def test_contract_terms_reuses_bounded_shared_rule_summary(self):
        market = {
            "ticker": "KXBTC15M-TEST",
            "floor_strike": 100000,
            "close_time": "2026-10-19T18:30:00Z",
            "rules_primary": "The final settlement uses an average over 2 minutes from CF Benchmarks.",
            "rules_secondary": "The market may close early if settlement is disrupted.",
        }
        terms = contract_terms(market)
        self.assertEqual(terms["strike"], 100000)
        self.assertEqual(terms["avg_seconds"], 120)
        self.assertEqual(terms["benchmark"], "CF Benchmarks")
        self.assertEqual(terms["rules_audit"]["status"], "review_required")
        self.assertIn("early_close", terms["rules_audit"]["flags"])
        self.assertLessEqual(terms["rules_audit"]["text_chars"], 6000)

    def test_missing_rules_are_not_invented(self):
        terms = contract_terms({"ticker": "KXTEST"})
        self.assertEqual(terms["rules_audit"]["status"], "unavailable")
        self.assertFalse(terms["rules_audit"]["available"])


if __name__ == "__main__":
    unittest.main()
