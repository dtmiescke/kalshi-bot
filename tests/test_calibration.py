"""The probability the bot bets on is Kalshi's price plus the share of its disagreement that was real."""
import os
import random
import tempfile
import unittest
from unittest import mock

from bot import brain, ml
from bot.storage import Store


def audit(store, n, make):
    """n graded 15-minute audit rows: make(i) -> (forecast, kalshi price, outcome)."""
    for i in range(n):
        p, k, y = make(i)
        store.x("""INSERT INTO decision_audit(ts,ticker,event_ticker,series,decision,market_price,model_prob,outcome,brier)
                   VALUES(?,?,?,?,?,?,?,?,?)""", (1_700_000_000 + i, f"KXBTC15M-T{i}", f"E{i}", "KXBTC15M", "research",
                                                  k, p, y, (p - y) ** 2))


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.d.cleanup()


class TestShrink(Base):
    def test_prior_until_there_is_enough_data_and_off_means_no_shrink(self):
        self.assertEqual(brain.shrink_weight(self.st, "KXBTC15M"), brain.SHRINK_PRIOR[0])
        self.assertEqual(brain.shrink_weight(self.st, "KXNFLGAME"), 1.0)         # no shrink for other domains by default
        with mock.patch.dict(os.environ, {"SHRINK_TO_MARKET": "no"}):
            self.st.__dict__.pop("_shrink_cache", None)
            self.assertEqual(brain.shrink_weight(self.st, "KXBTC15M"), 1.0)

    def test_a_few_windows_cannot_swing_the_weight_away_from_the_prior(self):
        audit(self.st, 40, lambda i: (0.9, 0.5, 1))                           # 40 windows that all agree with a big gap
        self.assertLess(brain.shrink_weight(self.st, "KXBTC15M"), 0.6)
        self.st.__dict__.pop("_shrink_cache")
        audit(self.st, 5, lambda i: (0.9, 0.5, 0))
        self.assertGreaterEqual(brain.shrink_weight(self.st, "KXBTC15M"), brain.SHRINK_RANGE[0])

    def test_a_forecast_that_is_noise_around_the_price_is_shrunk_hard(self):
        rng = random.Random(1)

        def noise(i):
            k = rng.uniform(0.2, 0.8)
            return min(0.99, max(0.01, k + rng.uniform(-0.25, 0.25))), k, int(rng.random() < k)
        audit(self.st, 600, noise)
        self.assertLess(brain.shrink_weight(self.st, "KXBTC15M"), 0.25)

    def test_a_forecast_that_knows_the_outcome_keeps_its_weight(self):
        rng = random.Random(2)

        def informed(i):
            k = rng.uniform(0.3, 0.7)
            y = int(rng.random() < k)
            return min(0.95, max(0.05, k + 0.3 * (y - k))), k, y      # the gap points the right way
        audit(self.st, 600, informed)
        self.assertGreater(brain.shrink_weight(self.st, "KXBTC15M"), 0.8)

    def test_shrink_moves_the_price_toward_the_forecast_by_that_share(self):
        self.st.__dict__["_shrink_cache"] = {"15m": (9e12, 0.25)}
        self.assertAlmostEqual(brain.shrink_to_market(0.90, 0.60, self.st, "KXBTC15M"), 0.675)
        self.assertAlmostEqual(brain.shrink_to_market(0.20, 0.60, self.st, "KXBTC15M"), 0.50)
        self.assertEqual(brain.shrink_to_market(None, 0.6, self.st, "KXBTC15M"), None)
        self.assertEqual(brain.shrink_to_market(0.7, None, self.st, "KXBTC15M"), 0.7)

    def test_ladders_are_not_shrunk_by_the_15_minute_record(self):
        audit(self.st, 100, lambda i: (0.9, 0.5, 0))                          # a terrible 15-minute record
        self.assertLess(brain.shrink_weight(self.st, "KXBTC15M"), 0.5)
        self.assertEqual(brain.shrink_weight(self.st, "KXBTCD"), 1.0)         # ladders keep their own record

    def test_prices_outside_the_betting_band_do_not_teach_the_weight(self):
        audit(self.st, 600, lambda i: (0.97, 0.99, 1))                          # decided markets: ignored
        self.assertEqual(brain.shrink_weight(self.st, "KXBTC15M"), brain.SHRINK_PRIOR[0])


class TestEdgeReport(Base):
    def test_reports_windows_errors_and_the_share_bet_on(self):
        import time
        from bot import dashboard
        now = int(time.time())
        for i in range(60):                      # recent settled windows, band prices
            self.st.x("""INSERT INTO decision_audit(ts,ticker,event_ticker,series,decision,market_price,model_prob,outcome,brier)
                         VALUES(?,?,?,?,?,?,?,?,?)""", (now - 100 - i, f"KXETH15M-W{i}", f"E{i}", "KXETH15M", "research",
                                                        0.5, 0.6, i % 2, (0.6 - i % 2) ** 2))
        self.st.x("""INSERT INTO decision_audit(ts,ticker,event_ticker,series,decision,market_price,model_prob,outcome,brier)
                     VALUES(?,?,?,?,?,?,?,?,?)""", (now, "KXBTCD-L1", "L", "KXBTCD", "research", 0.5, 0.9, 1, 0.01))   # a ladder: not counted
        e = dashboard.edge_report(self.st)
        self.assertEqual(e["windows"], 60)
        self.assertAlmostEqual(e["kalshi_brier"], 0.25)
        self.assertAlmostEqual(e["bot_brier"], 0.26, places=2)
        self.assertTrue(0.1 <= e["share"] <= 1.0)
        self.assertTrue(e["on"])


def ml_row(store, coin, source, i, model, kalshi, y):
    f = {"model_prob": model, "kalshi_logit": __import__("math").log(kalshi / (1 - kalshi)), "min_left": 8.0}
    ml.add_row(store, f"KX{coin}15M-{source}{i}", 1_700_000_000 + i, coin, f, y, source)


class TestUntrustedData(Base):
    def test_a_source_far_worse_than_kalshis_price_is_left_out_of_training(self):
        rng = random.Random(3)
        for i in range(400):
            y = int(rng.random() < 0.5)
            ml_row(self.st, "BNB", "history", i, 0.95 if rng.random() < .5 else 0.05, 0.5, y)   # confident and unrelated to y
            ml_row(self.st, "BTC", "history", i, min(.9, max(.1, 0.5 + .3 * (y - .5))), 0.5 + .1 * (y - .5), y)
            ml_row(self.st, "BNB", "live", i, 0.5, 0.5, y)
        bad = ml.untrusted_sources(self.st, now=1.0)
        self.assertEqual(bad, {("history", "BNB")})
        X, y, groups, ts, cols = ml.load_dataset(self.st)
        self.assertFalse(any("history0" in g and "BNB" in g for g in groups))
        self.assertTrue(any(g.startswith("KXBTC15M-history") for g in groups))
        self.assertTrue(any(g.startswith("KXBNB15M-live") for g in groups))

    def test_too_few_rows_are_never_judged(self):
        for i in range(50):
            ml_row(self.st, "SOL", "history", i, 0.95, 0.5, 0)
        self.assertEqual(ml.untrusted_sources(self.st, now=2.0), set())


if __name__ == "__main__":
    unittest.main()
