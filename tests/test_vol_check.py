"""Read-only volatility forecast check (history.vol_check)."""
import math
import os
import random
import tempfile
import unittest
from datetime import datetime

from bot import history
from bot.storage import Store

END = 1_800_000_000 - 1_800_000_000 % 900


def random_walk_get(sigma, seed=3, start=END - 2 * 86400, end=END + 3600):
    """Fake Coinbase candles API: a driftless random walk with per-minute sigma.
    Each minute's open/high/low/close come from 6 ten-second steps."""
    rnd, price, minutes = random.Random(seed), 100.0, {}
    for t in range(start, end, 60):
        path = [price]
        for _ in range(6):
            path.append(path[-1] * math.exp(rnd.gauss(0, sigma / math.sqrt(6))))
        minutes[t] = [t, min(path), max(path), path[0], path[-1], 1.0]
        price = path[-1]

    class Resp:
        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            pass

        def json(self):
            return self.rows

    def get(url, timeout=None, params=None):
        a = int(datetime.fromisoformat(params["start"]).timestamp())
        b = int(datetime.fromisoformat(params["end"]).timestamp())
        return Resp([minutes[t] for t in range(a, b, 60) if t in minutes])
    return get


class TestVolCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        history.time.sleep = lambda s: None   # spot_candles is polite between chunks

    def tearDown(self):
        import time as _t
        history.time.sleep = _t.sleep
        self.st.db.close()
        self.d.cleanup()

    def test_right_sized_forecast_scores_near_one(self):
        out = history.vol_check(self.st, days=1, series=["KXBTC15M"], now=END + 600,
                                spot_get=random_walk_get(0.0005))
        btc = out["coins"]["BTC"]
        self.assertEqual(btc["n"], 96 * 3)
        self.assertGreater(btc["vol_scale"], 0.85)
        self.assertLess(btc["vol_scale"], 1.15)
        self.assertAlmostEqual(btc["median_sigma_per_min"], 0.0005, delta=0.0001)
        self.assertEqual(set(btc["tails"]), {"1.0", "1.5", "2.0", "2.5", "3.0"})
        self.assertAlmostEqual(btc["tails"]["2.0"]["priced_at_0.9"], 0.0719, places=4)
        self.assertEqual(self.st.get_kv("vol_check_report")["coins"]["BTC"]["n"], btc["n"])

    def test_no_data_is_reported_not_raised(self):
        out = history.vol_check(self.st, days=1, series=["KXETH15M"], now=END + 600,
                                spot_get=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertEqual(out["coins"]["ETH"], {"n": 0})

    def test_changes_no_setting(self):
        history.vol_check(self.st, days=1, series=["KXBTC15M"], now=END + 600,
                          spot_get=random_walk_get(0.0005))
        for k in ("vol_blend_BTC", "tail_shrink_BTC"):
            self.assertIsNone(self.st.get_kv(k))


if __name__ == "__main__":
    unittest.main()
