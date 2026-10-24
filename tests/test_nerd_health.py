"""Nerd health aggregation must retain code-structure checks."""
import unittest

from bot.main import _attach_code_health


class TestNerdHealthAggregation(unittest.TestCase):
    def test_code_health_survives_database_recheck(self):
        health = {"ok": True, "checks": {"quick_check": ["ok"]}}
        code = {"ok": True, "required_paths": 16, "missing_paths": []}
        result = _attach_code_health(health, code)
        self.assertIs(result, health)
        self.assertEqual(result["checks"]["source_structure"], code)
        self.assertEqual(result["checks"]["quick_check"], ["ok"])

    def test_code_health_is_added_when_check_map_is_absent(self):
        result = _attach_code_health({"ok": True}, {"ok": False, "missing_paths": ["bot/x.py"]})
        self.assertEqual(result["checks"]["source_structure"]["ok"], False)


if __name__ == "__main__":
    unittest.main()
