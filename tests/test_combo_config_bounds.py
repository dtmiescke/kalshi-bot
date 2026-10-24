"""Combo leg configuration must stay within the supported 3-8 range."""
import unittest
from bot.config import Settings


class TestComboLegConfigBounds(unittest.TestCase):
    def test_default_combo_range_is_three_to_eight(self):
        settings = Settings(mode="paper")
        settings.validate()
        self.assertEqual(settings.combo_min_legs, 3)
        self.assertEqual(settings.combo_max_legs, 8)

    def test_minimum_leg_count_outside_supported_range_is_rejected(self):
        for value in (2, 9):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "COMBO_MIN_LEGS"):
                    Settings(mode="paper", combo_min_legs=value, combo_max_legs=8).validate()

    def test_maximum_leg_count_below_minimum_or_above_eight_is_rejected(self):
        for minimum, maximum in ((3, 2), (3, 9), (6, 5)):
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaisesRegex(ValueError, "COMBO_MAX_LEGS"):
                    Settings(mode="paper", combo_min_legs=minimum, combo_max_legs=maximum).validate()


if __name__ == "__main__":
    unittest.main()
