"""Regression tests for the read-only Kalshi contract-rules audit."""
import unittest

from bot.contract_rules import audit_rules
from bot.live.settlement import contract_terms


class TestContractRulesAudit(unittest.TestCase):
    def test_extracts_explicit_sources_and_flags_review_terms(self):
        result = audit_rules({
            "rules_primary": "Settlement is based on the CF Benchmarks index.",
            "rules_secondary": "If the event is suspended, it may close early.",
        })
        self.assertTrue(result["rules_present"])
        self.assertIn("CF Benchmarks", result["settlement_sources_named"])
        self.assertEqual(result["early_close_language"], "mentioned")
        self.assertIn("suspended", result["risk_terms_to_review"])
        self.assertIn("legal PDF not independently fetched", result["audit_scope"])

    def test_missing_rules_are_unknown_not_invented(self):
        result = audit_rules({})
        self.assertFalse(result["rules_present"])
        self.assertEqual(result["early_close_language"], "unknown")
        self.assertEqual(result["settlement_sources_named"], [])
        self.assertEqual(result["risk_terms_to_review"], [])

    def test_contract_terms_exposes_audit_without_changing_core_terms(self):
        result = contract_terms({
            "floor_strike": 100000,
            "close_time": "2026-10-19T18:30:00Z",
            "rules_primary": "The final settlement uses an average over 2 minutes from CF Benchmarks.",
        })
        self.assertEqual(result["strike"], 100000)
        self.assertEqual(result["avg_seconds"], 120)
        self.assertEqual(result["avg_source"], "rules")
        self.assertEqual(result["benchmark"], "CF Benchmarks")
        self.assertTrue(result["rules_audit"]["rules_present"])


if __name__ == "__main__":
    unittest.main()
