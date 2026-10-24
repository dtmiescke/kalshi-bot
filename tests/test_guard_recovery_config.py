"""Regression tests for safe recovery-window configuration parsing."""
import unittest
from unittest.mock import patch

from bot import guard


class TestRecoveryDaysConfiguration(unittest.TestCase):
    def test_default_and_valid_values(self):
        self.assertEqual(guard._recovery_days("21"), 21)
        self.assertEqual(guard._recovery_days("1"), 1)
        self.assertEqual(guard._recovery_days("365"), 365)

    def test_invalid_values_fall_back_to_safe_window(self):
        for raw in ("", "nope", "NaN", "1.5", None):
            with self.subTest(raw=raw):
                self.assertEqual(guard._recovery_days(raw), 21)

    def test_nonpositive_and_unbounded_values_cannot_disable_rolling_benches(self):
        for raw in ("0", "-1", "-21", "366", "999999"):
            with self.subTest(raw=raw):
                self.assertEqual(guard._recovery_days(raw), 21)

    def test_import_with_invalid_environment_does_not_crash_or_move_cutoff_forward(self):
        with patch.dict("os.environ", {"RECOVERY_DAYS": "-1"}):
            self.assertEqual(guard._recovery_days(), 21)


if __name__ == "__main__":
    unittest.main()
