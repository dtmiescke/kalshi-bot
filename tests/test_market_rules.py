import unittest

from bot.market_rules import summarize_rules


class TestMarketRules(unittest.TestCase):
    def test_missing_rules_are_explicitly_unavailable(self):
        result = summarize_rules({"ticker": "KXTEST"})
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["missing"])

    def test_primary_rules_and_early_close_are_flagged(self):
        result = summarize_rules({
            "rules_primary": "The market settles using the official index. The exchange may close early.",
            "rules_secondary": "A postponed event may be void.",
        })
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "review_required")
        self.assertIn("early_close", result["flags"])
        self.assertIn("source_or_index", result["flags"])
        self.assertIn("void_or_cancellation", result["flags"])
        self.assertIn("rules_primary", result["terms"])

    def test_raw_terms_are_bounded_and_not_inferred(self):
        result = summarize_rules({"rules_primary": "X" * 10000})
        self.assertLessEqual(len(result["terms"]["rules_primary"]), 3000)
        self.assertEqual(result["status"], "terms_found")
        self.assertNotIn("settlement_source", result["terms"])


if __name__ == "__main__":
    unittest.main()
