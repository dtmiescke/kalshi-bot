"""Arbitrage unwind fill confirmations fail closed on ambiguous responses."""
import unittest
from bot.engine import Engine


class TestUnwindFillConfirmation(unittest.TestCase):
    def test_valid_unwind_fill_is_bounded_by_original_position(self):
        self.assertEqual(Engine._confirmed_ioc_fill(
            {"order_id": "sell-1", "fill_count_fp": "2"}, 3), ("sell-1", 2))

    def test_unwind_confirmation_rejects_ambiguous_or_impossible_responses(self):
        for response in (
            None,
            {},
            {"fill_count_fp": "2"},
            {"order_id": "sell-2"},
            {"order_id": "sell-3", "fill_count_fp": "4"},
            {"order_id": "sell-4", "fill_count_fp": "0.5"},
        ):
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError):
                    Engine._confirmed_ioc_fill(response, 3)


if __name__ == "__main__":
    unittest.main()
