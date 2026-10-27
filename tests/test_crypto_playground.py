import unittest
from bot.live.crypto_playground import walk_forward_playground


def sample_bars(n=180):
    bars=[]
    price=100.0
    for i in range(n):
        drift=0.15 if (i//30)%2==0 else -0.12
        open_price=price
        close=max(1.0,price+drift)
        bars.append({"open":open_price,"high":max(open_price,close)+0.08,
                     "low":min(open_price,close)-0.08,"close":close,
                     "volume":100.0+(50.0 if i%15==0 else 0.0)})
        price=close
    return bars

class CryptoPlaygroundTests(unittest.TestCase):
    def test_walk_forward_keeps_test_period_separate(self):
        report=walk_forward_playground(sample_bars(), fee_bps=2, slippage_bps=3,
                                       train_fraction=0.6, threshold_grid=(0.25,0.5), min_trades=1)
        self.assertFalse(report["order_execution"])
        self.assertEqual(report["training_candles"], report["split_index"])
        self.assertEqual(report["test_candles"], 180-report["split_index"])
        self.assertIn("out_of_sample_metrics", report)
        self.assertEqual(report["cost_assumptions"]["fee_bps_per_side"], 2)

    def test_rejects_short_history(self):
        with self.assertRaises(ValueError):
            walk_forward_playground(sample_bars(70))

if __name__ == "__main__":
    unittest.main()
