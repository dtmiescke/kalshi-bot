import unittest
from unittest.mock import Mock

from bot.market_rules import compact_market_rules, fetch_market_rules


class TestMarketRules(unittest.TestCase):
    def test_compacts_available_rule_fields_and_preserves_unknowns_as_missing(self):
        result = compact_market_rules({
            "ticker": "KXTEST-26",
            "title": "Example market",
            "rules_primary": "Settles using the official league box score.",
            "settlement_source": "Official league website",
            "close_time": "2026-10-25T22:00:00Z",
            "irrelevant_large_field": "not copied",
        })
        self.assertEqual(result["ticker"], "KXTEST-26")
        self.assertIn("official league box score", result["rule_text"])
        self.assertEqual(result["rules"]["settlement_source"], "Official league website")
        self.assertNotIn("irrelevant_large_field", result["rules"])
        self.assertFalse(result["trusted_as_instructions"])

    def test_text_is_bounded_and_truncation_is_reported(self):
        result = compact_market_rules({"rules_primary": "x" * 100}, max_chars=20)
        self.assertLessEqual(len(result["rule_text"]), 20)
        self.assertTrue(result["truncated"])

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            compact_market_rules(None)
        with self.assertRaises(ValueError):
            compact_market_rules({}, max_chars=0)
        with self.assertRaises(ValueError):
            fetch_market_rules(Mock(), " ")

    def test_uses_existing_market_client_and_no_order_methods(self):
        client = Mock()
        client.market.return_value = {
            "ticker": "KXTEST-26",
            "rules_primary": "Primary rule",
            "rules_secondary": "Secondary rule",
        }
        result = fetch_market_rules(client, " KXTEST-26 ")
        client.market.assert_called_once_with("KXTEST-26")
        self.assertEqual(result["ticker"], "KXTEST-26")
        self.assertIn("Primary rule", result["rule_text"])
        client.place_resting_buy.assert_not_called()

    def test_missing_market_is_explicit_not_guessed(self):
        client = Mock()
        client.market.return_value = {}
        result = fetch_market_rules(client, "KXUNKNOWN")
        self.assertTrue(result["unavailable"])
        self.assertEqual(result["rules"], {})
        self.assertEqual(result["rule_text"], "")


if __name__ == "__main__":
    unittest.main()
