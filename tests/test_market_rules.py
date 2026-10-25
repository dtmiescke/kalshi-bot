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

    def test_series_metadata_supplies_legal_context_when_market_is_sparse(self):
        result = summarize_rules(
            {"ticker": "KXNFLGAME-TEST"},
            {
                "ticker": "KXNFLGAME",
                "settlement_sources": [{"name": "Official league result"}],
                "additional_prohibitions": ["Market-specific restrictions apply"],
                "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/contracts/example.pdf",
            },
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "review_required")
        self.assertIn("settlement_sources", result["terms"])
        self.assertIn("series_prohibition", result["flags"])
        self.assertEqual(len(result["official_rule_document_urls"]), 1)
        self.assertIn("example.pdf", result["official_rule_document_urls"][0])

    def test_unofficial_or_insecure_document_urls_are_not_trusted_references(self):
        result = summarize_rules(
            {"ticker": "KXTEST"},
            {
                "rules_primary": "Official settlement source is the league result.",
                "contract_url": "http://attacker.example/rules.pdf",
            },
        )
        self.assertEqual(result["official_rule_document_urls"], [])
        self.assertEqual(result["status"], "terms_found")

    def test_market_level_settlement_sources_are_supported(self):
        result = summarize_rules({
            "ticker": "KXTEST",
            "settlement_sources": ["Official data provider"],
        })
        self.assertTrue(result["available"])
        self.assertIn("settlement_sources", result["terms"])
        self.assertEqual(result["status"], "terms_found")


if __name__ == "__main__":
    unittest.main()
