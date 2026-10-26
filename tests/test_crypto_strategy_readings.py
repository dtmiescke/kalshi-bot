"""Supplemental crypto strategy signals are bounded and deterministic."""
import unittest

from bot.crypto_brain import strategy_readings


class TestCryptoStrategyReadings(unittest.TestCase):
    def test_emits_three_bounded_strategy_views(self):
        readings = strategy_readings({
            "ret_5m": 0.8, "ema_9_21": 0.4, "ema_trend_5m": 0.3,
            "adx_1m": 32, "rsi_1m": 68, "bb_pctb": 0.9,
            "day_high_dist": 0.05, "day_low_dist": 0.8, "vol_spike_5m": 2.5,
        }, 0.62)
        self.assertEqual(
            {r.name for r in readings},
            {"crypto_trend_strategy", "crypto_mean_reversion_strategy",
             "crypto_breakout_strategy"},
        )
        for reading in readings:
            self.assertGreaterEqual(reading.prob, 0.02)
            self.assertLessEqual(reading.prob, 0.98)
            self.assertGreater(reading.confidence, 0)
            self.assertLessEqual(reading.confidence, 1)

    def test_missing_features_returns_no_synthetic_signal(self):
        self.assertEqual(strategy_readings(None, 0.5), [])

    def test_bad_numeric_values_do_not_crash(self):
        readings = strategy_readings({"ret_5m": "bad", "adx_1m": float("nan")}, 0.5)
        self.assertEqual(len(readings), 3)
        self.assertTrue(all(0.02 <= r.prob <= 0.98 for r in readings))


if __name__ == "__main__":
    unittest.main()
