"""Fitted tail model: Student-t shape + vol scale, used only after it beats
current pricing on newer windows."""
import math
import os
import random
import tempfile
import unittest

from scipy.special import stdtr

from bot import vol
from bot.live import settlement
from bot.storage import Store

ARGS = (99700.0, 100000.0, 0.0006, 420, 60)


def t_samples(df, scale, n_windows, per=3, seed=5, start=0):
    """z whose z/scale is unit-variance Student-t(df), grouped in 15-minute windows."""
    rnd, out = random.Random(seed), []
    k = math.sqrt((df - 2) / df)
    for w in range(n_windows):
        for _ in range(per):
            # Student-t draw: normal / sqrt(chi2/df)
            chi2 = sum(rnd.gauss(0, 1) ** 2 for _ in range(df))
            out.append((start + 900 * w, scale * k * rnd.gauss(0, 1) / math.sqrt(chi2 / df)))
    return out


class TestPricing(unittest.TestCase):
    def test_student_t_pricing_formula(self):
        z = settlement.z_above(ARGS[0], ARGS[1], ARGS[2] * 0.9, ARGS[3], ARGS[4])
        p = settlement.prob_above(*ARGS, tail_df=4, vol_scale=0.9)
        self.assertAlmostEqual(p, float(stdtr(4, z * math.sqrt(2))), places=12)

    def test_without_model_pricing_is_unchanged(self):
        self.assertEqual(settlement.prob_above(*ARGS, tail_shrink=0.9),
                         settlement.prob_above(*ARGS, tail_shrink=0.9, tail_df=None, vol_scale=1.0))

    def test_long_shots_cheaper_far_tails_richer(self):
        # strike placed so the move needed is ~1.5 sd (a 5-10c long shot) and ~3.5 sd
        sd = 0.0006 * math.sqrt((420 - 60 + 20) / 60)
        for z_needed, cheaper in ((1.5, True), (3.5, False)):
            spot = 100000.0 * math.exp(-z_needed * sd)
            old = settlement.prob_above(spot, 100000.0, 0.0006, 420, 60, tail_shrink=0.9, proxy_bps=0)
            new = settlement.prob_above(spot, 100000.0, 0.0006, 420, 60, tail_df=4, vol_scale=0.98,
                                        proxy_bps=0)
            (self.assertLess if cheaper else self.assertGreater)(new, old, z_needed)

    def test_invalid_shape_returns_none(self):
        for kw in ({"tail_df": 2}, {"tail_df": float("nan")}, {"vol_scale": 0},
                   {"vol_scale": -1}, {"tail_df": "x"}):
            self.assertIsNone(settlement.prob_above(*ARGS, **kw), kw)


class TestFit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def test_recovers_peaked_shape_and_is_accepted(self):
        details = vol.learn_tail_model(self.st, "BTC", t_samples(4, 0.9, 500))
        self.assertTrue(details["accepted"], details)
        self.assertIn(details["df"], (3, 4, 5, 6))
        self.assertAlmostEqual(details["vol_scale"], 0.9, delta=0.06)
        self.assertGreaterEqual(details["holdout_windows"], vol.MIN_HOLDOUT_WINDOWS)
        self.assertEqual(vol.tail_model(self.st, "BTC"), (details["df"], details["vol_scale"]))

    def test_rejected_when_newer_windows_disagree(self):
        # older windows peaked/narrow, newer windows a plain normal 1/0.9 wide,
        # i.e. exactly today's pricing: the old fit must not win on new data
        old = t_samples(3, 0.75, 350)
        rnd = random.Random(9)
        new = [(900 * (350 + w), rnd.gauss(0, 1 / 0.9)) for w in range(150) for _ in range(3)]
        details = vol.learn_tail_model(self.st, "ETH", old + new)
        self.assertFalse(details["accepted"], details)
        self.assertIsNone(vol.tail_model(self.st, "ETH"))

    def test_needs_enough_windows(self):
        details = vol.learn_tail_model(self.st, "SOL", t_samples(4, 0.9, 80))
        self.assertFalse(details["accepted"])
        self.assertIn("not enough windows", details["reason"])
        self.assertIsNone(vol.tail_model(self.st, "SOL"))

    def test_stored_model_is_validated(self):
        for bad in ({"accepted": False, "df": 4, "vol_scale": 0.9},
                    {"accepted": True, "df": 1.5, "vol_scale": 0.9},
                    {"accepted": True, "df": 4, "vol_scale": 3.0},
                    {"accepted": True, "df": "x", "vol_scale": 0.9}):
            self.st.set_kv("tail_model_BTC", bad)
            self.assertIsNone(vol.tail_model(self.st, "BTC"), bad)


class TestCryptoBrainUsesModel(unittest.TestCase):
    def test_reading_priced_with_accepted_model(self):
        from bot import crypto_brain
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            now = 1_800_000_000
            candles = [{"ts": now - (300 - i) * 60, "open": 100000 + (i % 7) * 4,
                        "high": 100010 + (i % 7) * 4, "low": 99990 + (i % 7) * 4,
                        "close": 100000 + (i % 7) * 4, "volume": 1 + i % 3} for i in range(300)]
            terms = {"strike": 100050.0, "close_ts": now + 420, "avg_seconds": 60}
            kw = dict(now=now, spot=100010.0, dvol_fetch=lambda c: None, with_context=False,
                      log_row=False)
            before, info = crypto_brain.research(st, "KXBTC15M-W-00", "KXBTC15M", terms, candles,
                                                 0.40, 0.44, **kw)
            st.set_kv("tail_model_BTC", {"accepted": True, "df": 4, "vol_scale": 0.9})
            after, _ = crypto_brain.research(st, "KXBTC15M-W-00", "KXBTC15M", terms, candles,
                                             0.40, 0.44, **kw)
            cm_before = next(r for r in before if r.name == "crypto_model")
            cm_after = next(r for r in after if r.name == "crypto_model")
            want = settlement.prob_above(100010.0, 100050.0, info["sigma"], 420, 60,
                                         tail_df=4, vol_scale=0.9)
            self.assertAlmostEqual(cm_after.prob, max(0.02, min(0.98, want)))
            self.assertEqual((cm_after.detail["tail_df"], cm_after.detail["vol_scale"]), (4, 0.9))
            self.assertIsNone(cm_before.detail["tail_df"])
            st.db.close()


if __name__ == "__main__":
    unittest.main()
