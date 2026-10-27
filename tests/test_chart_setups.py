import unittest
from bot.live.chart_setups import analyze_chart_setups

class ChartSetupTests(unittest.TestCase):
    def test_short_input_is_not_ready(self):
        result = analyze_chart_setups([])
        self.assertFalse(result["ready"])

    def test_detects_breakout(self):
        bars = []
        for i in range(30):
            p = 100.0 + i * 0.1
            bars.append({"open": p - 0.02, "high": p + 0.03, "low": p - 0.04, "close": p, "volume": 100.0})
        bars[-1] = {"open": 103.0, "high": 104.2, "low": 102.9, "close": 104.0, "volume": 300.0}
        result = analyze_chart_setups(bars, book_imbalance=0.4, aggressor_imbalance=0.3)
        names = {row["name"] for row in result["setups"]}
        self.assertIn("range_breakout", names)
        self.assertIn("orderbook_imbalance", names)

if __name__ == "__main__":
    unittest.main()
