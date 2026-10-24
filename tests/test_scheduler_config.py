"""Fail early on malformed scheduler environment settings."""
import os
import unittest
from unittest.mock import patch

from bot.config import Settings


class TestSchedulerEnvironmentValidation(unittest.TestCase):
    def test_invalid_float_scheduler_settings_fail_validation(self):
        cases = {
            "SPORTS_EVERY_HOURS": "0",
            "BURST_MINUTES": "NaN",
            "TRAIN_EVERY_HOURS": "-1",
            "TRAIN_RETRY_HOURS": "inf",
        }
        for key, value in cases.items():
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(ValueError, key):
                    Settings(mode="paper").validate()

    def test_invalid_integer_scheduler_settings_fail_validation(self):
        cases = {
            "ML_MIN_WINDOWS": "0",
            "SPORTS_SCAN_EVENT_BUDGET": "bad",
            "SPORTS_INTEL_EVENT_BUDGET": "-2",
            "SPORTS_AI_NEWS_REFRESH_MINUTES": "1.5",
        }
        for key, value in cases.items():
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(ValueError, key):
                    Settings(mode="paper").validate()

    def test_valid_zero_burst_and_positive_scheduler_defaults_are_allowed(self):
        with patch.dict(os.environ, {"BURST_MINUTES": "0"}, clear=False):
            Settings(mode="paper").validate()


if __name__ == "__main__":
    unittest.main()
