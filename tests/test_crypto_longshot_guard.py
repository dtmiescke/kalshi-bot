"""Task 1: bounded fat-tail adjustment and the crypto long-shot guard.

PR #10 left an open question: early 15-minute YES bets at 11-17c when the
settlement model said ~1%. An unbounded tail_shrink fit (down to 0.5) inflates
a 1-2% contract to 10-13%, and nothing stopped the bot buying it.
"""
import math
import os
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

    def test_fit_cannot_leave_bounds(self):
        # Long shots that "hit" far too often would pull an unbounded fit to 0.5.
        rows = [(0.01, 1 if i % 4 == 0 else 0) for i in range(400)]
        got = vol.learn_tail_shrink(self.st, "BTC", rows)
        self.assertEqual(got, vol.TAIL_SHRINK_MIN)
        self.assertEqual(vol.tail_shrink(self.st, "BTC"), vol.TAIL_SHRINK_MIN)
        # Ultra-thin tails can't push it above the cap either.
        rows = [(0.2, 0)] * 300 + [(0.8, 1)] * 300
        self.assertEqual(vol.learn_tail_shrink(self.st, "ETH", rows), vol.TAIL_SHRINK_MAX)

    def test_fit_skips_bad_rows_and_needs_enough_data(self):
        rows = [(float("nan"), 1), (None, 0), (0.5, 2)] * 100 + [(0.3, 0)] * 150
        self.assertIsNone(vol.learn_tail_shrink(self.st, "SOL", rows))   # only 150 usable
        self.assertIsNone(self.st.get_kv("tail_shrink_SOL"))

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
                                    min_edge=0.05, max_per_bet=10)
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
