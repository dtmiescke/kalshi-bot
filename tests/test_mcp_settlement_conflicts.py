"""Regression tests for read-only MCP contract-rule envelopes."""
import unittest

from bot.market_rules import summarize_rules


class TestMcpSettlementConflicts(unittest.TestCase):
    def test_conflicting_nested_settlement_sources_require_review(self):
        payload = {
            "settlement_source": "Official source A",
            "rules_primary": "Settles according to official source A",
            "structuredContent": {
                "settlement_source": "Official source B",
                "rules_primary": "Untrusted nested text",
            },
        }
        result = summarize_rules(payload)
        self.assertEqual(result["status"], "review_required")
        self.assertIn("mcp_settlement_source_conflict", result["flags"])
        self.assertEqual(result["terms"]["settlement_source"], "Official source A")

    def test_matching_nested_sources_do_not_create_false_conflict(self):
        payload = {
            "settlement_source": "Official source A",
            "rules_primary": "Settles according to official source A",
            "structuredContent": {"settlement_source": "official SOURCE a"},
        }
        result = summarize_rules(payload)
        self.assertNotIn("mcp_settlement_source_conflict", result["flags"])

    def test_conflicting_series_envelope_requires_review(self):
        market = {"settlement_source": "Source A", "rules_primary": "Rule"}
        series = {"structuredContent": {
            "settlement_source": "Source A",
            "result": {"settlement_source": "Source B"},
        }}
        result = summarize_rules(market, series)
        self.assertEqual(result["status"], "review_required")
        self.assertIn("mcp_settlement_source_conflict", result["flags"])


if __name__ == "__main__":
    unittest.main()
