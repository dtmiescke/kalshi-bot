"""Supplemental crypto strategy signals are bounded and deterministic."""
import unittest

from bot.crypto_brain import strategy_readings
from bot.features import timeframe_indicator_snapshot


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

    def test_timeframe_snapshot_includes_30m_after_sufficient_history(self):
        import math
        candles = []
        for i in range(1200):
            close = 100 + i * 0.015 + math.sin(i / 9) * 0.8
            candles.append({
                "ts": 1_700_000_000 + i * 60,
                "open": close - 0.05, "high": close + 0.25,
                "low": close - 0.25, "close": close, "volume": 10 + (i % 7),
            })
        snapshot = timeframe_indicator_snapshot(candles)
        self.assertEqual(set(snapshot), {"1m", "5m", "15m", "30m"})
        for tf in ("1m", "5m", "15m", "30m"):
            self.assertIn("rsi", snapshot[tf])
            self.assertIn("macd_hist_bps", snapshot[tf])
            self.assertIn("adx", snapshot[tf])
            self.assertTrue(math.isfinite(snapshot[tf]["rsi"]))

    def test_30m_is_omitted_when_history_is_insufficient(self):
        import math
        candles = []
        for i in range(300):
            close = 100 + i * 0.01 + math.sin(i / 7) * 0.2
            candles.append({
                "ts": 1_700_000_000 + i * 60,
                "open": close - 0.02, "high": close + 0.1,
                "low": close - 0.1, "close": close, "volume": 5,
            })
        self.assertNotIn("30m", timeframe_indicator_snapshot(candles))

    def test_missing_features_returns_no_synthetic_signal(self):
        self.assertEqual(strategy_readings(None, 0.5), [])

    def test_bad_numeric_values_do_not_crash(self):
        readings = strategy_readings({"ret_5m": "bad", "adx_1m": float("nan")}, 0.5)
        self.assertEqual(len(readings), 3)
        self.assertTrue(all(0.02 <= r.prob <= 0.98 for r in readings))


if __name__ == "__main__":
    unittest.main()
