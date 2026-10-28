"""Spot cross-check: our spot price vs Hyperliquid's oracle (a median of
several exchanges). A big gap halves the price models' confidence; no fresh
reference means no check (never blocks)."""
import os
import tempfile
import unittest
from unittest import mock

from bot import crypto_brain, marketdata
from bot.signals.base import Reading
from bot.storage import Store

NOW = 1_800_000_000.0


class TestSpotCheck(unittest.TestCase):
    def ctx(self, ref=80000.0, age=10):
        return {"ref_spot": ref, "ref_spot_ts": NOW - age}

    def test_small_gap_passes(self):
        c = crypto_brain.spot_check(80080.0, self.ctx(), NOW, max_bps=40)
        self.assertEqual(c["gap_bps"], 10.0)
        self.assertTrue(c["ok"])

    def test_big_gap_fails(self):
        c = crypto_brain.spot_check(80800.0, self.ctx(), NOW, max_bps=40)
        self.assertEqual(c["gap_bps"], 100.0)
        self.assertFalse(c["ok"])

    def test_stale_or_missing_reference_is_no_check(self):
        self.assertIsNone(crypto_brain.spot_check(80800.0, self.ctx(age=600), NOW))
        self.assertIsNone(crypto_brain.spot_check(80800.0, {}, NOW))
        self.assertIsNone(crypto_brain.spot_check(80800.0, self.ctx(ref=None), NOW))
        self.assertIsNone(crypto_brain.spot_check(None, self.ctx(), NOW))

    def test_threshold_from_env(self):
        with mock.patch.dict(os.environ, {"SPOT_CHECK_MAX_BPS": "5"}):
            self.assertFalse(crypto_brain.spot_check(80080.0, self.ctx(), NOW)["ok"])


class TestResearchUsesCheck(unittest.TestCase):
    """Run research() with stubbed pricing so only the cross-check varies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_research(self, ref):
        candles = [{"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1.0,
                    "ts": int(NOW) - 60 * (40 - i)} for i in range(40)]
        terms = {"strike": 100.0, "close_ts": NOW + 600, "avg_seconds": 60}
        ctx = {"ref_spot": ref, "ref_spot_ts": NOW - 5}
        with mock.patch.object(crypto_brain.volmod, "forecast", return_value=(0.001, {})), \
                mock.patch.object(crypto_brain.volmod, "tail_shrink", return_value=0.0), \
                mock.patch.object(crypto_brain.volmod, "tail_model", return_value=None), \
                mock.patch.object(crypto_brain.iv, "cached_dvol", return_value=None), \
                mock.patch.object(crypto_brain.features, "build", return_value=None), \
                mock.patch.object(crypto_brain.features, "timeframe_indicator_snapshot", return_value={}), \
                mock.patch.object(crypto_brain, "indicator_snapshot", return_value={}):
            readings, info = crypto_brain.research(self.store, "KXBTC15M-X", "KXBTC15M", terms, candles,
                                                   0.45, 0.55, now=NOW, context=ctx, log_row=False)
        return {r.name: r for r in readings}, info

    def test_agreeing_reference_keeps_confidence(self):
        r, info = self.run_research(100.05)
        self.assertTrue(info["spot_check"]["ok"])
        self.assertEqual(r["crypto_model"].confidence, 0.75)
        self.assertIn("spot_gap_bps", r["crypto_model"].detail)

    def test_disagreeing_reference_halves_price_model_confidence(self):
        r, info = self.run_research(101.0)
        self.assertFalse(info["spot_check"]["ok"])
        self.assertEqual(r["crypto_model"].confidence, 0.375)
        self.assertTrue(r["crypto_model"].detail["spot_check_failed"])


class TestContextCarriesReference(unittest.TestCase):
    def test_oracle_and_timestamp_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.sqlite"))

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return [{"universe": [{"name": "SOL"}]},
                            [{"funding": "0.0000125", "premium": "0", "oraclePx": "101.27"}]]
            hl = marketdata.hyperliquid(store, "SOL", post=lambda *a, **k: R())
            self.assertEqual(hl["oracle"], 101.27)
            self.assertIsInstance(hl["ts"], float)


if __name__ == "__main__":
    unittest.main()
