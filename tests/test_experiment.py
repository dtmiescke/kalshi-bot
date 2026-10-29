"""The experiment lane: loosened rules at the real prices, in a shadow account that cannot touch anything else."""
import os
import tempfile
import unittest

from bot import experiment, readiness
from bot.storage import Store


def asks(yes, no):
    return {"yes": yes, "no": no}


class TestDecide(unittest.TestCase):
    def test_takes_the_side_whose_edge_after_the_real_fee_clears_the_bar(self):
        fill = experiment.decide(0.70, asks([(0.55, 100), (0.58, 100)], [(0.47, 100)]))
        self.assertEqual(fill["side"], "yes")
        self.assertEqual(fill["price"], 0.55)
        self.assertGreaterEqual(fill["edge"], experiment.MIN_EDGE)
        self.assertGreaterEqual(fill["contracts"], 1)
        self.assertLessEqual(fill["contracts"] * fill["price"], experiment.STAKE + 1e-9)

    def test_takes_no_when_the_forecast_says_down(self):
        fill = experiment.decide(0.30, asks([(0.45, 100)], [(0.52, 100)]))
        self.assertEqual(fill["side"], "no")
        self.assertAlmostEqual(fill["p_win"], 0.70, places=3)

    def test_no_trade_when_the_edge_is_below_the_bar_or_fees_eat_it(self):
        self.assertIsNone(experiment.decide(0.52, asks([(0.51, 100)], [(0.50, 100)])))
        self.assertIsNone(experiment.decide(0.55, asks([(0.53, 100)], [(0.49, 100)])))     # 2 points less fee: below 3%

    def test_missing_forecast_or_book_never_trades(self):
        self.assertIsNone(experiment.decide(None, asks([(0.5, 10)], [(0.5, 10)])))
        self.assertIsNone(experiment.decide(0.8, None))
        self.assertIsNone(experiment.decide(0.8, asks([], [])))

    def test_fills_walk_the_real_depth_and_never_chase_past_two_cents(self):
        thin = asks([(0.50, 2), (0.60, 100)], [(0.60, 100)])       # only 2 contracts near the ask; the rest is far worse
        fill = experiment.decide(0.80, thin)
        self.assertEqual(fill["contracts"], 2)                      # the 60-cent depth is beyond the chase limit
        self.assertEqual(fill["price"], 0.50)

    def test_unexecutable_extreme_prices_are_skipped(self):
        self.assertIsNone(experiment.decide(0.999, asks([(0.01, 1000)], [(0.99, 1000)])))


class TestLane(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.lane = experiment.Experiment(self.store)

    def grade(self, ticker, outcome):
        self.store.x("INSERT OR REPLACE INTO graded_markets VALUES(?,?,?,?)", (ticker, 1, outcome, "live"))

    def test_one_shadow_entry_per_market_then_settle_and_report(self):
        t = "KXBTC15M-26OCT292345-45"
        a = asks([(0.55, 100)], [(0.47, 100)])
        self.assertIsNotNone(self.lane.observe(t, "KXBTC15M", 1_000, 8.0, 0.70, a))
        self.assertIsNone(self.lane.observe(t, "KXBTC15M", 1_030, 7.5, 0.70, a))            # already in this market
        rep = experiment.report(self.store)
        self.assertEqual((rep["entries"], rep["settled"], rep["open"]), (1, 0, 1))
        self.grade(t, 1)
        rep = experiment.report(self.store)
        self.assertEqual((rep["settled"], rep["wins"]), (1, 1))
        self.assertGreater(rep["pnl"], 0)
        self.assertGreater(rep["per100"], 0)
        self.assertEqual(rep["by_coin"]["BTC"]["n"], 1)

    def test_a_losing_shadow_trade_costs_the_stake_and_the_fee(self):
        t = "KXETH15M-26OCT292345-45"
        fill = self.lane.observe(t, "KXETH15M", 1_000, 8.0, 0.70, asks([(0.55, 100)], [(0.47, 100)]))
        self.grade(t, 0)
        self.assertEqual(experiment.settle(self.store), 1)
        row = self.store.q("SELECT * FROM experiment_bets WHERE ticker=?", (t,))[0]
        self.assertEqual(row["status"], "settled")
        self.assertAlmostEqual(row["pnl"], -(fill["contracts"] * fill["price"] + fill["fee"]), places=2)

    def test_empty_report_and_failures_are_safe(self):
        rep = experiment.report(self.store)
        self.assertEqual(rep["entries"], 0)
        self.assertNotIn("pnl", rep)
        self.assertIsNone(self.lane.observe("t", "KXBTC15M", 1, 8.0, 0.7, "not a book"))      # a bad book never raises

    def test_it_cannot_touch_the_real_ledger_or_the_checklist(self):
        t = "KXBTC15M-26OCT292345-45"
        self.lane.observe(t, "KXBTC15M", 1_000, 8.0, 0.70, asks([(0.55, 100)], [(0.47, 100)]))
        self.grade(t, 1)
        experiment.report(self.store)
        self.assertEqual(self.store.q("SELECT COUNT(*) AS n FROM bets")[0]["n"], 0)          # nothing in the real bets table
        self.assertEqual(readiness.report(self.store, {})["bets"], 0)                         # nor in the go-live checks


if __name__ == "__main__":
    unittest.main()


class TestRunnerWiring(unittest.TestCase):
    """LiveRunner.experiment_step on a minimal stand-in: records, throttles, and never raises or trades."""

    def setUp(self):
        import types
        from bot.live import runner
        from bot.signals.base import Reading
        self.runner, self.Reading, self.types = runner, Reading, types
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.t = "KXBTC15M-26OCT292345-45"

    class Book:
        def __init__(self, age=1.0):
            self._age = age

        def age(self):
            return self._age

        def as_rest_book(self):
            return {"yes": [(0.45, 500)], "no": [(0.50, 500)]}          # YES ask 50c, NO ask 55c

    def stub(self, book):
        return self.types.SimpleNamespace(experiment=experiment.Experiment(self.store), exp_next={},
                                          books={self.t: book} if book else {}, store=self.store)

    def step(self, stub, now=1000.0, readings=None):
        r = [self.Reading("crypto_model", 0.80, 0.9, {})] if readings is None else readings
        self.runner.LiveRunner.experiment_step(stub, self.t, {"series": "KXBTC15M"}, r, {}, now, 8.0)

    def rows(self):
        experiment.ensure_table(self.store)                           # the table only exists once something was recorded
        return self.store.q("SELECT * FROM experiment_bets")

    def test_it_records_one_shadow_entry_from_the_live_book(self):
        s = self.stub(self.Book())
        self.step(s)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["side"], "yes")

    def test_it_looks_at_most_every_five_seconds(self):
        s = self.stub(self.Book())
        self.step(s, now=1000.0, readings=[])                         # no readings: nothing recorded, but the look is spent
        self.step(s, now=1002.0)                                      # inside the 5 s throttle: skipped
        self.assertEqual(len(self.rows()), 0)
        self.step(s, now=1006.0)
        self.assertEqual(len(self.rows()), 1)

    def test_a_stale_or_missing_book_or_the_off_switch_does_nothing(self):
        self.step(self.stub(self.Book(age=30.0)))
        self.step(self.stub(None))
        os.environ["EXPERIMENT_LANE"] = "no"
        try:
            self.step(self.stub(self.Book()))
        finally:
            del os.environ["EXPERIMENT_LANE"]
        self.assertEqual(len(self.rows()), 0)

    def test_the_real_ledger_is_untouched(self):
        self.step(self.stub(self.Book()))
        self.assertEqual(self.store.q("SELECT COUNT(*) AS n FROM bets")[0]["n"], 0)
