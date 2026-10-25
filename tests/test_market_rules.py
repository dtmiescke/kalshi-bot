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

    def test_nested_kalshi_market_envelope_is_unwrapped(self):
        result = summarize_rules({
            "market": {
                "ticker": "KXTEST",
                "rules_primary": "Settles from the official index.",
                "settlement_source": "Official index",
            },
            "series_prohibitions": ["No trading after event cancellation."],
        })
        self.assertTrue(result["available"])
        self.assertIn("rules_primary", result["terms"])
        self.assertIn("series_prohibitions", result["terms"])
        self.assertIn("source_or_index", result["flags"])

    def test_mcp_consolidated_contract_text_is_supported(self):
        result = summarize_rules({
            "result": {
                "rules_pdf_text": "Contract settles at the official source unless postponed.",
            }
        })
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "terms_found")
        self.assertIn("rules_pdf_text", result["terms"])
        self.assertIn("void_or_cancellation", result["flags"])

    def test_third_party_mcp_rules_aliases_are_normalized(self):
        result = summarize_rules({
            "data": {
                "market": {
                    "ticker": "KXTEST",
                    "rules": "The market resolves using the official index.",
                    "contract_terms": "A canceled event may be void.",
                    "prohibitions": ["Trading prohibited after close."],
                }
            }
        })
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "terms_found")
        self.assertIn("rules", result["terms"])
        self.assertIn("contract_terms", result["terms"])
        self.assertIn("prohibitions", result["terms"])
        self.assertIn("source_or_index", result["flags"])
        self.assertIn("void_or_cancellation", result["flags"])
        self.assertIn("series_prohibition", result["flags"])

    def test_alias_text_is_bounded_and_does_not_bypass_missing_rule_guard(self):
        result = summarize_rules({
            "contract_terms": "C" * 5000,
            "prohibitions": ["P" * 5000],
        })
        self.assertLessEqual(len(result["terms"]["contract_terms"]), 3000)
        self.assertLessEqual(len(result["terms"]["prohibitions"]), 3000)
        self.assertFalse(result["missing"])
        self.assertEqual(result["status"], "terms_found")

    def test_non_dict_or_empty_mcp_result_fails_closed(self):
        for payload in (None, [], "not a market", {"result": None}):
            result = summarize_rules(payload)
            self.assertFalse(result["available"])
            self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
