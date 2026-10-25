import json
import unittest

from bot.market_rules import summarize_rules


class TestMarketRulesEnvelopeHardening(unittest.TestCase):
    def test_stringified_result_envelope_is_parsed(self):
        payload = {
            "result": json.dumps({
                "data": {
                    "market": {
                        "rules_primary": "Settles using the official league result.",
                        "settlement_source": "Official league result",
                    }
                }
            })
        }
        result = summarize_rules(payload)
        self.assertEqual(result["status"], "terms_found")
        self.assertEqual(result["terms"]["settlement_source"], "Official league result")

    def test_oversized_stringified_envelope_does_not_become_authoritative(self):
        payload = {
            "result": "{" + (" " * 20001) + '"rules_primary":"Settles using official source"}'
        }
        result = summarize_rules(payload)
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "unavailable")

    def test_excessive_text_blocks_fail_closed(self):
        payload = {
            "content": [
                {"type": "text", "text": "plain evidence block"}
                for _ in range(9)
            ]
        }
        result = summarize_rules(payload)
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "unavailable")

    def test_plain_text_evidence_has_a_global_character_bound(self):
        payload = {
            "content": [
                {"type": "text", "text": "A" * 5000},
                {"type": "text", "text": "B" * 5000},
            ]
        }
        result = summarize_rules(payload)
        self.assertTrue(result["available"])
        self.assertLessEqual(len(result["terms"]["rules_pdf_text"]), 6000)
        self.assertTrue(result["missing"])
        self.assertEqual(result["status"], "review_required")

    def test_nested_error_inside_stringified_result_fails_closed(self):
        payload = {
            "rules_primary": "Outer wrapper must not override a tool error.",
            "result": json.dumps({
                "isError": True,
                "content": [{"type": "text", "text": "tool failed"}],
            }),
        }
        result = summarize_rules(payload)
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "unavailable")

    def test_conflicting_mcp_series_settlement_source_requires_review(self):
        market = {"rules_primary": "Settles on official final result.",
                  "settlement_source": "League official website"}
        series = {"structuredContent": {"result": {"data": {
            "settlement_sources": "Third-party unofficial statistics provider"
        }}}}
        result = summarize_rules(market, series)
        self.assertEqual(result["status"], "review_required")
        self.assertIn("settlement_source_conflict", result["flags"])
        self.assertEqual(result["terms"]["settlement_source"], "League official website")

    def test_matching_mcp_series_settlement_source_is_not_conflict(self):
        market = {"rules_primary": "Settles on official final result.",
                  "settlement_source": "League official website"}
        series = {"result": {"settlement_sources": "League official website"}}
        result = summarize_rules(market, series)
        self.assertEqual(result["status"], "terms_found")
        self.assertNotIn("settlement_source_conflict", result["flags"])

    def test_too_many_envelope_nodes_fail_closed(self):
        def tree(depth):
            if depth == 0:
                return {"rules_primary": "Must not be trusted past the traversal bound."}
            return {"result": tree(depth - 1), "data": tree(depth - 1)}

        result = summarize_rules(tree(6))
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
