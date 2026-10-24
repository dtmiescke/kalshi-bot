"""Tests for the sports-specific counsel handoff."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import council
from bot.storage import Store


class TestSportsCounsel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "bot.sqlite"))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_advisory_mode_falls_back_to_deterministic_checks_without_llm(self):
        with patch.dict(os.environ, {"COUNCIL_MODE": "advisory"}):
            result = council.review_sports(None, self.store, "KXNFL-MARKET", {"edge": 0.1})
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["by"], "numbers")

    def test_required_mode_waits_when_counsel_is_unavailable(self):
        with patch.dict(os.environ, {"COUNCIL_MODE": "required"}):
            result = council.review_sports(None, self.store, "KXNFL-MARKET", {"edge": 0.1})
        self.assertEqual(result["decision"], "wait")

    def test_off_mode_is_explicitly_numbers_only(self):
        with patch.dict(os.environ, {"COUNCIL_MODE": "off"}):
            result = council.review_sports(None, self.store, "KXNFL-MARKET", {"edge": 0.1})
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["by"], "numbers")

    def test_prompts_format_without_brace_errors(self):
        self.assertIn("KXNFL", council.SPORTS_COUNCIL_PROMPT.format(summary="KXNFL"))
        self.assertIn("KXNFL", council.SPORTS_SKEPTIC_PROMPT.format(summary="KXNFL"))


if __name__ == "__main__":
    unittest.main()
