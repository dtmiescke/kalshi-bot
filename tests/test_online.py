"""The running scoreboard: scored before it learns, starts as Kalshi's price, and flags a market shift."""
import math
import os
import random
import tempfile
import unittest

from bot import ml, online
from bot.storage import Store


def feats(rng, signal=0.0, mid=None):
    p = mid if mid is not None else rng.uniform(0.25, 0.75)
    f = {"kalshi_logit": math.log(p / (1 - p)), "model_prob": min(max(p + rng.uniform(-.1, .1), .02), .98),
         "dist_sigma": rng.gauss(0, 1), "ret_5m": signal, "ret_15m": rng.gauss(0, 1), "rsi_15m": rng.uniform(30, 70),
         "macd_hist": rng.gauss(0, 1), "adx_1m": rng.uniform(10, 40), "vol_ratio_15_60": rng.uniform(.7, 1.3),
         "min_left": 12.0}
    return f, p


def windows(n, seed, strength=0.0, flip_after=None):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        sig = rng.gauss(0, 1)
        f, p = feats(rng, signal=sig)
        s = -strength if (flip_after is not None and i >= flip_after) else strength
        true_p = 1 / (1 + math.exp(-(f["kalshi_logit"] + s * sig)))
        out.append((1000 + i, f, int(rng.random() < true_p)))
    return out


class TestModel(unittest.TestCase):
    def test_it_starts_as_kalshis_price(self):
        f, p = feats(random.Random(1), mid=0.62)
        self.assertAlmostEqual(online.OnlineLogit().predict(f), 0.62, places=6)
        self.assertIsNone(online.OnlineLogit().predict({"model_prob": .5}))      # no Kalshi price, no opinion

    def test_noise_does_not_look_like_skill(self):
        diffs, lk, lo, m = online.run(windows(600, seed=2, strength=0.0))
        mean, se = online._mean_se(diffs)
        self.assertLess(abs(mean / se), 2.5)
        self.assertLess(abs(sum(lo) / len(lo) - sum(lk) / len(lk)), 0.02)         # barely moves off Kalshi
        self.assertAlmostEqual(m.kw, 1.0, delta=0.15)

    def test_a_real_signal_is_learned_and_scored_out_of_sample(self):
        diffs, lk, lo, m = online.run(windows(800, seed=3, strength=1.2))
        mean, se = online._mean_se(diffs)
        self.assertGreater(mean / se, 2.5)                                        # beats Kalshi on windows it had not yet learned from
        self.assertGreater(m.w["ret_5m"], 0.1)

    def test_a_flipped_market_raises_the_drift_alarm(self):
        rows = windows(600, seed=4, strength=1.5, flip_after=500)                  # the signal reverses for the last 100 windows
        diffs, *_ = online.run(rows)
        d = online.drift(diffs)
        self.assertTrue(d["alarm"], d)
        self.assertLess(d["gain_per_window"], 0)
        steady, *_ = online.run(windows(600, seed=4, strength=1.5))
        self.assertFalse(online.drift(steady)["alarm"])
        self.assertIsNone(online.drift(steady[:150]))                            # too few windows to say


class TestReport(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.d.cleanup()

    def test_an_empty_store_is_safe(self):
        r = online.report(self.st)
        self.assertEqual(r["online"], {"windows": 0})
        self.assertEqual(r["bot"], {"windows": 0})

    def test_it_scores_each_window_once_at_its_earliest_decision(self):
        rng = random.Random(6)
        for i in range(40):
            for left in (13.0, 9.0, 4.0):                                          # three reads of the same window
                f, p = feats(rng)
                f["min_left"] = left
                ml.add_row(self.st, f"KXBTC15M-W{i}", 1000 + i * 100 - int(left), "BTC", f, int(rng.random() < p), "live")
        rows = online.decisions(self.st)
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(abs(f["min_left"] - 13.0) < 1e-9 for _, f, _ in rows))
        r = online.report(self.st)
        self.assertEqual(r["online"]["windows"], 40)
        self.assertIn("kalshi_logloss", r["online"])

    def test_the_bots_own_forecast_is_scored_from_the_audit(self):
        import datetime as dt
        rng = random.Random(7)
        base = 1_793_200_000 - (1_793_200_000 % 900)
        for i in range(40):
            close = base + 900 * i
            et = dt.datetime.fromtimestamp(close, dt.timezone.utc) - dt.timedelta(hours=4)
            ticker = f"KXBTC15M-{et.strftime('%y%b%d%H%M').upper()}-{et.strftime('%M')}"
            y = int(rng.random() < .5)
            for left in (12.0, 6.0):                                               # two snapshots: the earlier counts
                self.st.x("""INSERT INTO decision_audit(ts,ticker,event_ticker,series,decision,market_price,model_prob,outcome,brier)
                             VALUES(?,?,?,?,?,?,?,?,?)""", (int(close - left * 60), ticker, ticker, "KXBTC15M", "research",
                                                             .5, .5 + (.2 if y else -.2) * (1 if left == 12.0 else 0), y, 0.1))
        r = online.report(self.st)
        self.assertEqual(r["bot"]["windows"], 40)
        self.assertGreater(r["bot"]["gain_per_window"], 0)                          # its early forecast pointed the right way


if __name__ == "__main__":
    unittest.main()
