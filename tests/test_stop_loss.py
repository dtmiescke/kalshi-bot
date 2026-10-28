"""Cash a bet out before it loses more than 20% of what it cost."""
import unittest
from unittest.mock import patch

from bot import risk
from bot.config import Settings


class TestStopLoss(unittest.TestCase):
    def test_default_limit_and_buffer(self):
        s = Settings(mode="paper")
        self.assertAlmostEqual(s.max_loss_per_bet, 0.20)
        self.assertAlmostEqual(s.stop_loss_buffer, 0.05)
        with patch.dict("os.environ", {"MAX_LOSS_PER_BET": "10", "STOP_LOSS_BUFFER": "2"}):
            s = Settings(mode="paper")
            self.assertAlmostEqual((s.max_loss_per_bet, s.stop_loss_buffer), (0.10, 0.02))
        with patch.dict("os.environ", {"MAX_LOSS_PER_BET": "10", "STOP_LOSS_BUFFER": "10"}):
            with self.assertRaises(ValueError):
                Settings(mode="paper").validate()

    def test_trigger_counts_both_fees_and_sits_inside_the_limit(self):
        # cost 0.60 + 0.017 fee; selling at 0.50 less a 0.017 fee is -19% -> out
        self.assertTrue(risk.stop_loss_hit(0.60, 0.017, 0.50, 0.017))
        # trigger is at 15% net: 0.55 -> -13.6% holds, 0.54 -> -15.4% sells
        self.assertFalse(risk.stop_loss_hit(0.60, 0.017, 0.55, 0.017))
        self.assertTrue(risk.stop_loss_hit(0.60, 0.017, 0.54, 0.017))
        self.assertFalse(risk.stop_loss_hit(0.60, 0.017, None, 0.017))

    def test_sweep_takes_levels_best_first(self):
        self.assertEqual(risk.sweep_bids([(0.50, 10), (0.49, 10), (0.48, 10)], 25), (25, 0.492, 0.48))
        self.assertEqual(risk.sweep_bids([(0.50, 10)], 25), (10, 0.5, 0.5))       # sells what exists
        self.assertEqual(risk.sweep_bids([], 5), (0, None, None))


if __name__ == "__main__":
    unittest.main()
