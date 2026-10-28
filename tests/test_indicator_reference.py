"""The bot's indicators agree with pandas-ta, an independent, widely used implementation.

The expected numbers were produced once with pandas-ta 0.4.71b0 (pandas 3.1, numpy 2.2) on the
deterministic series below, which uses only math functions so it is the same everywhere. pandas-ta is
not a dependency of the bot (it pulls in numba and pins numpy); this test keeps the result."""
import math
import unittest

import numpy as np

from bot import features as F


def series(n=300):
    close, p = [], 100000.0
    for i in range(n):
        p *= math.exp(0.0008 * math.sin(i * 0.7) + 0.0004 * math.cos(i * 0.13) + 0.00005 * math.sin(i * 2.3))
        close.append(p)
    openp = [close[0]] + close[:-1]
    high = [max(o, c) * (1 + 0.0003 * (1 + math.sin(i * 1.9))) for i, (o, c) in enumerate(zip(openp, close))]
    low = [min(o, c) * (1 - 0.0003 * (1 + math.cos(i * 1.1))) for i, (o, c) in enumerate(zip(openp, close))]
    vol = [5 + 4 * math.sin(i * 0.37) * math.cos(i * 0.11) for i in range(n)]
    return [np.array(x) for x in (openp, high, low, close, vol)]


REF = {"rsi14": 72.619598, "macd_line": 101.809983, "macd_hist": 42.518242, "atr14": 119.697871,
       "bb_pctb": 0.962347, "ema9": 100310.243808, "ema21": 100190.085323, "adx14": 24.816328, "obv": -0.27236}


class TestAgainstPandasTa(unittest.TestCase):
    def setUp(self):
        _, self.h, self.l, self.c, self.v = series()

    def test_rsi_atr_adx(self):
        self.assertAlmostEqual(F.rsi(self.c, 14), REF["rsi14"], places=4)
        self.assertAlmostEqual(F.atr(self.h, self.l, self.c, 14), REF["atr14"], places=4)
        self.assertAlmostEqual(F.adx(self.h, self.l, self.c, 14), REF["adx14"], places=4)

    def test_ema_and_macd(self):
        self.assertAlmostEqual(F.ema(self.c, 9)[-1], REF["ema9"], places=3)
        self.assertAlmostEqual(F.ema(self.c, 21)[-1], REF["ema21"], places=3)
        line = F.ema(self.c, 12) - F.ema(self.c, 26)
        self.assertAlmostEqual(line[-1], REF["macd_line"], places=3)
        self.assertAlmostEqual(line[-1] - F.ema(line, 9)[-1], REF["macd_hist"], places=3)

    def test_bollinger_percent_b_uses_the_sample_standard_deviation(self):
        w = self.c[-20:]
        sd = w.std(ddof=1)
        self.assertAlmostEqual((self.c[-1] - (w.mean() - 2 * sd)) / (4 * sd), REF["bb_pctb"], places=5)

    def test_on_balance_volume(self):
        obv = np.cumsum(np.sign(np.diff(self.c)) * self.v[1:])
        self.assertAlmostEqual(float(obv[-1]), REF["obv"], places=3)

    def test_the_feature_builder_reports_the_same_rsi_and_adx(self):
        o, h, l, c, v = series()
        candles = [{"ts": 1_700_000_000 + 60 * i, "open": o[i], "high": h[i], "low": l[i], "close": c[i], "volume": v[i]}
                   for i in range(len(c))]
        f = F.build(candles[-300:], strike=c[-1], secs_left=600, now_ts=candles[-1]["ts"])
        self.assertAlmostEqual(f["adx_1m"], REF["adx14"], places=3)
        self.assertAlmostEqual(f["rsi_1m"], REF["rsi14"], places=3)


if __name__ == "__main__":
    unittest.main()
