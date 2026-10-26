"""Task 1: bounded fat-tail adjustment and the crypto long-shot guard.

PR #10 left an open question: early 15-minute YES bets at 11-17c when the
settlement model said ~1%. An unbounded tail_shrink fit (down to 0.5) inflates
a 1-2% contract to 10-13%, and nothing stopped the bot buying it.
"""
import math
import os
import random
import tempfile
import unittest
from unittest import mock

from bot import risk, vol
from bot.config import Settings
from bot.engine import Engine
from bot.live import settlement
from bot.signals.base import Reading
from bot.storage import Store

CRYPTO = {"ticker": "KXBTC15M-W-00", "event_ticker": "KXBTC15M-W", "series": "KXBTC15M"}
# YES ask $0.12 (NO bid 0.88), NO ask $0.89 (YES bid 0.11)
LONGSHOT_BOOK = {"yes": [(0.11, 500)], "no": [(0.88, 500)]}


class TestTailShrinkBounds(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.d.cleanup()

    def test_stored_values_are_clamped_or_defaulted(self):
        self.assertEqual(vol.tail_shrink(self.st, "BTC"), 0.9)            # nothing learned yet
        for stored, want in ((0.5, vol.TAIL_SHRINK_MIN), (3.0, vol.TAIL_SHRINK_MAX),
                             (0.95, 0.95), (float("nan"), 0.9), ("junk", 0.9), (0, 0.9),
                             (-1, 0.9)):
            self.st.x("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)",
                      ("tail_shrink_BTC", "NaN" if isinstance(stored, float) and math.isnan(stored)
                       else __import__("json").dumps(stored)))
            self.assertEqual(vol.tail_shrink(self.st, "BTC"), want, stored)

    @staticmethod
    def windows(shrinks, n_windows, rows_per=6, seed=7, start=0):
        """Synthetic settled windows: outcome ~ Bernoulli(Phi(true_shrink * z))."""
        from statistics import NormalDist
        rnd, nd, out = random.Random(seed), NormalDist(), []
        for w in range(n_windows):
            true = shrinks(w) if callable(shrinks) else shrinks
            for i in range(rows_per):
                z = rnd.uniform(-3, 3)
                y = 1 if rnd.random() < nd.cdf(true * z) else 0
                out.append((f"KXBTC15M-W{start + w}", 1000 * (start + w) + i, z, y))
        return out

    def test_needs_enough_windows_and_keeps_current_value(self):
        details = vol.learn_tail_shrink(self.st, "BTC", self.windows(0.82, 90))
        self.assertFalse(details["accepted"])
        self.assertIn("not enough windows", details["reason"])
        self.assertIsNone(self.st.get_kv("tail_shrink_BTC"))         # nothing stored
        self.assertEqual(self.st.get_kv("tail_shrink_fit_BTC")["windows"], 90)

    def test_accepts_fit_that_beats_default_on_newer_windows(self):
        details = vol.learn_tail_shrink(self.st, "BTC", self.windows(0.80, 600, rows_per=25))
        self.assertTrue(details["accepted"], details)
        self.assertGreaterEqual(details["holdout_windows"], vol.MIN_HOLDOUT_WINDOWS)
        self.assertLess(details["holdout_logloss_candidate"], details["holdout_logloss_default"])
        self.assertAlmostEqual(vol.tail_shrink(self.st, "BTC"), details["candidate"])
        self.assertLess(details["candidate"], 0.88)
        self.assertEqual(self.st.get_kv("tail_shrink_fit_BTC")["value"], details["candidate"])

    def test_rejects_fit_that_does_not_hold_on_newer_windows(self):
        # older windows look fat-tailed, newer ones thin: the old fit doesn't hold up
        rows = self.windows(lambda w: 0.80 if w < 280 else 1.05, 400, rows_per=10)
        details = vol.learn_tail_shrink(self.st, "ETH", rows)
        self.assertFalse(details["accepted"], details)
        self.assertEqual(vol.tail_shrink(self.st, "ETH"), 0.9)
        self.assertEqual(self.st.get_kv("tail_shrink_ETH"), 0.9)

    def test_fit_cannot_leave_bounds(self):
        # truth far fatter than allowed: the fit stops at the floor, never 0.5
        details = vol.learn_tail_shrink(self.st, "SOL", self.windows(0.5, 400, rows_per=10))
        self.assertEqual(details["candidate"], vol.TAIL_SHRINK_MIN)
        self.assertGreaterEqual(vol.tail_shrink(self.st, "SOL"), vol.TAIL_SHRINK_MIN)

    def test_thin_tails_fit_up_to_ceiling(self):
        # owner spec 0.8-1.2: a thin-tailed truth above the old 1.05 cap is reachable
        details = vol.learn_tail_shrink(self.st, "BTC", self.windows(1.15, 600, rows_per=25))
        self.assertTrue(details["accepted"], details)
        self.assertGreater(details["candidate"], 1.05)
        self.assertLessEqual(details["candidate"], vol.TAIL_SHRINK_MAX)
        details = vol.learn_tail_shrink(self.st, "ETH", self.windows(1.6, 400, rows_per=10))
        self.assertEqual(details["candidate"], vol.TAIL_SHRINK_MAX)
        self.assertEqual(vol.TAIL_SHRINK_MAX, 1.2)

    def test_window_split_is_by_time_and_counts_windows_once(self):
        rows = self.windows(0.82, 200)
        rows += [("KXBTC15M-W0", 5, 0.1, 1)] * 500        # many rows in one old window
        details = vol.learn_tail_shrink(self.st, "BTC", rows)
        self.assertEqual(details["windows"], 200)
        self.assertEqual(details["holdout_windows"], 60)  # newest 30%

    def test_bad_rows_are_skipped(self):
        rows = [("A", 1, float("nan"), 1), ("B", 2, None, 0), ("C", 3, 0.5, 2),
                ("D", 4, 0.5, True), ("E", "x", 0.5, 1)]
        details = vol.learn_tail_shrink(self.st, "BTC", rows)
        self.assertEqual(details["windows"], 0)

    def test_learn_tails_uses_live_z_rows_of_current_version_only(self):
        import json
        from bot import features, ml
        ml.ensure_table(self.st)
        for i, (version, feats) in enumerate((
                (features.FEATURE_VERSION - 1, {"live_z": 1.0}),   # old version
                (features.FEATURE_VERSION, {"model_prob": 0.3}),   # no live z
                (features.FEATURE_VERSION, {"live_z": -0.5}))):    # usable
            self.st.x("INSERT INTO ml_rows VALUES(?,?,?,?,?,?,?)",
                      (f"KXBTC15M-W{i}", i, "BTC", version, json.dumps(feats), 1, "live"))
        out = ml.learn_tails(self.st)
        self.assertEqual(out["BTC"]["windows"], 1)
        self.assertFalse(out["BTC"]["accepted"])

    def test_z_above_matches_prob_above(self):
        from statistics import NormalDist
        args = (99400, 100000, 0.00076, 600, 60)
        z = settlement.z_above(*args, proxy_bps=3)
        self.assertAlmostEqual(settlement.prob_above(*args, tail_shrink=0.85, proxy_bps=3),
                               NormalDist().cdf(0.85 * z), places=12)
        self.assertEqual(settlement.z_above(101, 100, 1e-12, 0, 60, realized_avg=101,
                                            proxy_bps=0), math.inf)
        self.assertIsNone(settlement.z_above(-1, 100, 0.01, 30))
        self.assertIsNone(settlement.prob_above(*args, tail_shrink=float("nan")))

    def test_bounded_shrink_keeps_one_percent_contract_near_fair(self):
        # Spot 0.6% under strike, 10 minutes left, 0.076%/min vol: a ~1% contract.
        args = (99400, 100000, 0.00076, 600, 60)
        fair = settlement.prob_above(*args, tail_shrink=0.9, proxy_bps=0)
        broken = settlement.prob_above(*args, tail_shrink=0.5, proxy_bps=0)
        bounded = settlement.prob_above(*args, tail_shrink=vol.TAIL_SHRINK_MIN, proxy_bps=0)
        self.assertLess(fair, 0.02)
        self.assertGreater(broken, 0.09)                 # ~10c: the old 11-17c failure mode
        self.assertLess(bounded, 0.04)


class TestCryptoLongshotGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()
        self.s.mode = "paper"

    def tearDown(self):
        self.d.cleanup()

    def test_blocks_cheap_crypto_side_when_raw_model_disagrees(self):
        # Blend says 30% on a 12c YES, but the raw contract model says 2%.
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.30, 0.9, LONGSHOT_BOOK,
                                min_edge=0.05, raw_model_prob=0.02)
        self.assertIsNone(plan)
        self.assertIn("crypto long shot", why)
        self.assertIn("raw model 2.0%", why)

    def test_blocks_cheap_crypto_side_without_raw_model(self):
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.30, 0.9, LONGSHOT_BOOK, min_edge=0.05)
        self.assertIsNone(plan)
        self.assertIn("no raw model", why)

    def test_allows_cheap_crypto_side_when_raw_model_agrees(self):
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.30, 0.9, LONGSHOT_BOOK,
                                min_edge=0.05, raw_model_prob=0.30)
        self.assertIsNotNone(plan, why)
        self.assertEqual((plan["side"], plan["price"]), ("yes", 0.12))

    def test_cheap_no_side_uses_raw_model_complement(self):
        # NO ask 12c: raw YES 0.98 means NO is a 2% long shot -> blocked.
        book = {"yes": [(0.88, 500)], "no": [(0.11, 500)]}
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.70, 0.9, book,
                                min_edge=0.05, raw_model_prob=0.98)
        self.assertIsNone(plan)
        self.assertIn("NO @ $0.12", why)

    def test_hourly_ladders_are_crypto_too(self):
        ladder = {"ticker": "KXBTCD-26OCT2417-T100000", "event_ticker": "KXBTCD-26OCT2417",
                  "series": "KXBTCD"}
        plan, why = risk.decide(self.s, self.st, ladder, 0.30, 0.9, LONGSHOT_BOOK,
                                min_edge=0.05, raw_model_prob=0.02)
        self.assertIsNone(plan)
        self.assertIn("crypto long shot", why)

    def test_sports_and_normal_prices_unaffected(self):
        sports = {"ticker": "KXNBAGAME-X-BOS", "event_ticker": "KXNBAGAME-X", "series": "KXNBAGAME"}
        plan, why = risk.decide(self.s, self.st, sports, 0.30, 0.9, LONGSHOT_BOOK, min_edge=0.05)
        self.assertIsNotNone(plan, why)
        mid_book = {"yes": [(0.40, 500)], "no": [(0.55, 500)]}         # YES ask 45c
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.60, 0.9, mid_book, min_edge=0.05)
        self.assertIsNotNone(plan, why)

    def test_exactly_twenty_cents_is_not_a_long_shot(self):
        # refuse below $0.20; $0.20 itself is allowed without raw-model support
        book = {"yes": [(0.19, 500)], "no": [(0.80, 500)]}             # YES ask $0.20
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.40, 0.9, book,
                                min_edge=0.05, raw_model_prob=0.02)
        self.assertIsNotNone(plan, why)
        self.assertEqual(plan["price"], 0.20)
        book = {"yes": [(0.18, 500)], "no": [(0.81, 500)]}             # YES ask $0.19
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.40, 0.9, book,
                                min_edge=0.05, raw_model_prob=0.02)
        self.assertIsNone(plan)
        self.assertIn("crypto long shot", why)

    def test_threshold_zero_disables_guard(self):
        self.s.crypto_longshot_price = 0.0
        plan, why = risk.decide(self.s, self.st, CRYPTO, 0.30, 0.9, LONGSHOT_BOOK,
                                min_edge=0.05, raw_model_prob=0.02)
        self.assertIsNotNone(plan, why)

    def test_config_validation(self):
        with mock.patch.dict(os.environ, {"CRYPTO_LONGSHOT_PRICE": "0.6"}):
            with self.assertRaisesRegex(ValueError, "CRYPTO_LONGSHOT_PRICE"):
                Settings().validate()
        with mock.patch.dict(os.environ, {"CRYPTO_LONGSHOT_PRICE": "nan"}):
            with self.assertRaisesRegex(ValueError, "CRYPTO_LONGSHOT_PRICE"):
                Settings().validate()
        self.assertEqual(Settings().crypto_longshot_price, 0.20)


class TestEngineWiring(unittest.TestCase):
    """The snapshot path hands the raw crypto_model reading to the guard."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()
        self.s.mode = "paper"

        class Data:
            def orderbook(self, ticker):
                return LONGSHOT_BOOK
        self.eng = Engine(self.s, self.st, data_client=Data(), llm=False)

    def tearDown(self):
        self.d.cleanup()

    def _evaluate(self, raw):
        readings = [Reading("kalshi_market", 0.115, 0.9, {"mid": 0.115}),
                    Reading("crypto_model", raw, 0.75, {})]
        summary = {"skipped": {}}
        with mock.patch("bot.engine.brain.blend", return_value=(0.30, 0.9)), \
                mock.patch("bot.engine.brain.calibrate", side_effect=lambda p, *a, **k: p):
            got = self.eng.evaluate(dict(CRYPTO), readings, {}, summary, required="crypto_model",
                                    min_edge=0.05, max_per_bet=20)
        return got, summary

    def test_market_anchored_blend_cannot_buy_a_model_long_shot(self):
        got, summary = self._evaluate(0.02)
        self.assertIsNone(got)
        reasons = [r["reason"] for r in self.st.q("SELECT reason FROM decision_audit")]
        self.assertTrue(any("crypto long shot" in r for r in reasons), reasons)

    def test_supported_long_shot_still_trades(self):
        got, _ = self._evaluate(0.30)
        self.assertIsNotNone(got)
        self.assertEqual(got[1]["side"], "yes")


if __name__ == "__main__":
    unittest.main()


class TestTailValueLogged(unittest.TestCase):
    def test_prediction_and_training_row_record_tail_value_and_z(self):
        import json
        from bot import crypto_brain
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            st.set_kv("tail_shrink_BTC", 0.85)
            now = 1_800_000_000
            candles = [{"ts": now - (300 - i) * 60, "open": 100000 + (i % 7) * 4,
                        "high": 100010 + (i % 7) * 4, "low": 99990 + (i % 7) * 4,
                        "close": 100000 + (i % 7) * 4, "volume": 1 + i % 3} for i in range(300)]
            terms = {"strike": 100050.0, "close_ts": now + 420, "avg_seconds": 60}
            readings, info = crypto_brain.research(
                st, "KXBTC15M-W-00", "KXBTC15M", terms, candles, 0.40, 0.44, now=now,
                spot=100010.0, dvol_fetch=lambda c: None, with_context=False)
            cm = next(r for r in readings if r.name == "crypto_model")
            self.assertEqual(cm.detail["tail_shrink"], 0.85)
            z = settlement.z_above(100010.0, 100050.0, info["sigma"], 420, 60)
            self.assertAlmostEqual(cm.detail["z"], round(z, 5))
            row = json.loads(st.q("SELECT features FROM ml_rows")[0]["features"])
            self.assertAlmostEqual(row["live_z"], z)
            self.assertEqual(row["tail_shrink_used"], 0.85)
            st.db.close()
