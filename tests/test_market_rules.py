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
        self.assertEqual(result["status"], "review_required")
        self.assertTrue(result["missing"])
        self.assertIn("rules_pdf_text", result["terms"])
        self.assertIn("void_or_cancellation", result["flags"])

    def test_series_metadata_supplies_settlement_source_and_prohibitions(self):
        result = summarize_rules(
            {"ticker": "KXNFLGAME-TEST"},
            {
                "ticker": "KXNFLGAME",
                "settlement_sources": [{"name": "Official league result"}],
                "additional_prohibitions": ["Special series restrictions apply"],
                "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/contracts/example.pdf",
            },
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "review_required")
        self.assertIn("settlement_sources", result["terms"])
        self.assertIn("series_prohibition", result["flags"])
        self.assertEqual(len(result["official_rule_document_urls"]), 1)

    def test_untrusted_contract_document_url_is_not_exposed(self):
        result = summarize_rules(
            {"rules_primary": "Official settlement source is the league result."},
            {"contract_url": "https://attacker-kalshi.amazonaws.com/rules.pdf"},
        )
        self.assertEqual(result["official_rule_document_urls"], [])

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
        self.assertEqual(result["status"], "review_required")
        self.assertIn("rules", result["terms"])
        self.assertIn("contract_terms", result["terms"])
        self.assertIn("prohibitions", result["terms"])
        self.assertIn("source_or_index", result["flags"])
        self.assertIn("void_or_cancellation", result["flags"])
        self.assertIn("series_prohibition", result["flags"])

    def test_alias_fields_are_bounded_and_prohibitions_alone_do_not_count_as_rules(self):
        result = summarize_rules({
            "contract_terms": "C" * 5000,
            "prohibitions": ["P" * 5000],
        })
        self.assertLessEqual(len(result["terms"]["contract_terms"]), 3000)
        self.assertLessEqual(len(result["terms"]["prohibitions"]), 3000)
        self.assertFalse(result["missing"])
        self.assertEqual(result["status"], "review_required")
        self.assertIn("series_prohibition", result["flags"])
        prohibited_only = summarize_rules({"prohibitions": ["P" * 5000]})
        self.assertTrue(prohibited_only["missing"])
        self.assertEqual(prohibited_only["status"], "review_required")

    def test_may_close_early_phrase_requires_review(self):
        result = summarize_rules({
            "rules_primary": "The market may close early if settlement is disrupted.",
            "settlement_source": "Official exchange index",
        })
        self.assertEqual(result["status"], "review_required")
        self.assertIn("early_close", result["flags"])

    def test_pdf_text_without_native_settlement_source_still_requires_review(self):
        result = summarize_rules({
            "rules_pdf_text": "Official contract text says the event settles after completion."
        })
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "review_required")
        self.assertTrue(result["missing"])

    def test_non_dict_or_empty_mcp_result_fails_closed(self):
        for payload in (None, [], "not a market", {"result": None}):
            result = summarize_rules(payload)
            self.assertFalse(result["available"])
            self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
