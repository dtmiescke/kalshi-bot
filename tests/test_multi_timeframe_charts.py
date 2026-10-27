import unittest
from bot.live.chart_setups import analyze_multi_timeframe, resample_candles


class MultiTimeframeTests(unittest.TestCase):
    def test_resampling_preserves_ohlcv_aggregation(self):
        bars = [
            {"ts": i * 60, "open": 100+i, "high": 101+i, "low": 99+i,
             "close": 100.5+i, "volume": 10}
            for i in range(10)
        ]
        out = resample_candles(bars, 300)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["open"], bars[0]["open"])
        self.assertEqual(out[0]["close"], bars[4]["close"])
        self.assertEqual(out[0]["high"], bars[4]["high"])
        self.assertEqual(out[0]["low"], bars[0]["low"])
        self.assertEqual(out[0]["volume"], 50)

    def test_multitimeframe_result_is_explicit_when_insufficient_history(self):
        bars = [{"ts": i*60, "open":100, "high":101, "low":99, "close":100.5, "volume":10}
                for i in range(25)]
        result = analyze_multi_timeframe(bars)
        self.assertIn("1m", result["timeframes"])
        self.assertIn("30m", result["timeframes"])
        self.assertEqual(result["directional_alignment"], "mixed_or_insufficient")


if __name__ == "__main__":
    unittest.main()
