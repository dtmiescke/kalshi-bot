"""Simulated markets with a known truth: the closed-form formula, the fee drag, and how long it takes to know."""
import os
import tempfile
import unittest

from bot import simulate
from bot.storage import Store


class TestFormula(unittest.TestCase):
    def test_closed_form_share(self):
        self.assertEqual(simulate.lambda_star(0.0, 0.3), 0.0)
        self.assertAlmostEqual(simulate.lambda_star(0.3, 0.3), 0.5)
        self.assertGreater(simulate.lambda_star(0.3, 0.1), simulate.lambda_star(0.3, 0.6))     # a sharper forecast earns more trust

    def test_an_efficient_market_cannot_be_beaten_after_fees(self):
        rows = simulate.world(0.0, 0.3, 20000, seed=3)
        for lam in (0.25, 1.0):
            s = simulate.stats(simulate.pnl(rows, 0.05, lam))
            self.assertTrue(s["n"] == 0 or s["mean"] < 0, (lam, s))                              # it either stays out or only pays fees
        best = simulate.best_rule(rows)
        self.assertTrue(best is None or best[2]["mean"] < 0)

    def test_the_right_share_beats_blind_trust_when_the_market_errs(self):
        rows = simulate.world(0.3, 0.3, 30000, seed=4)
        right = simulate.stats(simulate.pnl(rows, 0.05, simulate.lambda_star(0.3, 0.3)))
        blind = simulate.stats(simulate.pnl(rows, 0.05, 1.0))
        self.assertGreater(right["mean"], blind["mean"])
        self.assertGreater(right["mean"], 0)

    def test_fitted_share_recovers_the_truth_with_an_honest_error_bar(self):
        rows = simulate.world(0.3, 0.3, 4000, seed=5)
        est, se = simulate.lambda_fit(rows)
        self.assertLess(abs(est - 0.5), 2.5 * se)
        none, nse = simulate.lambda_fit(simulate.world(0.0, 0.3, 4000, seed=6))
        self.assertLess(abs(none), 2.5 * nse)                                                    # no inefficiency, no share

    def test_how_many_bets_it_takes_to_know(self):
        self.assertIsNone(simulate.bets_to_detect(-0.01, 0.5))
        self.assertGreater(simulate.bets_to_detect(0.01, 0.5), simulate.bets_to_detect(0.05, 0.5))
        self.assertEqual(simulate.windows_to_pin(0.65, 111, 0.1), 4690)

    def test_efficient_market_gap_is_about_zero(self):
        rows = [(k, y) for k, _, y in simulate.world(0.0, 0.3, 6000, seed=7)]
        g = simulate.market_gap(rows)
        self.assertLess(abs(g["excess_logloss"]), 3 * g["se"])
        self.assertIsNone(simulate.market_gap([]))


class TestReport(unittest.TestCase):
    def test_empty_store_is_safe(self):
        r = simulate.report(Store(os.path.join(tempfile.mkdtemp(), "t.sqlite")))
        self.assertIsNone(r["market_gap"])
        self.assertNotIn("lam_fit", r)

    def test_the_bots_own_estimator_stays_near_its_prior_on_a_few_windows(self):
        learned = simulate.estimator_check(0.0, 0.3, n_windows=200, seed=2)
        self.assertGreater(learned, 0.1)
        self.assertLessEqual(learned, 1.0)


if __name__ == "__main__":
    unittest.main()
