"""Sportsbooks vs Kalshi: who prices settled games better, and does following the books when they disagree pay?"""
import os
import random
import tempfile
import unittest

from bot import sports_study
from bot.storage import Store


class TestSportsStudy(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.rng = random.Random(4)
        self.n = 0

    def game(self, league, truth, book_noise, kalshi_noise):
        """One settled game with both signals. truth is the real win chance; each price is truth plus its own error."""
        self.n += 1
        t = f"{league}-26OCT30G{self.n}-A"
        y = 1 if self.rng.random() < truth else 0
        for sig, noise in (("book_consensus", book_noise), ("kalshi_market", kalshi_noise)):
            p = min(max(truth + self.rng.gauss(0, noise), 0.03), 0.97)
            self.store.x("INSERT INTO signal_grades(ts, bet_id, signal, prob, outcome, brier, clv_direction, segment) "
                         "VALUES(?,?,?,?,?,?,?,?)", (1, None, sig, p, y, (p - y) ** 2, None, t))

    def test_empty_and_small_samples_report_only_the_count(self):
        self.assertEqual(sports_study.report(self.store)["games"], 0)
        for _ in range(10):
            self.game("KXNBAGAME", 0.6, 0.02, 0.02)
        rep = sports_study.report(self.store)
        self.assertEqual(rep["games"], 10)
        self.assertNotIn("all", rep)

    def test_equally_good_prices_do_not_look_like_an_edge(self):
        for _ in range(300):
            self.game("KXNBAGAME", self.rng.uniform(0.2, 0.8), 0.04, 0.04)
        rep = sports_study.report(self.store)
        self.assertEqual(rep["all"]["games"], 300)
        self.assertLess(abs(rep["all"]["z"]), 3.0)

    def test_books_that_really_are_sharper_show_up_with_a_positive_gain(self):
        for _ in range(2000):
            self.game("KXNFLGAME", self.rng.uniform(0.15, 0.85), 0.01, 0.12)         # books near the truth, Kalshi noisy
        rep = sports_study.report(self.store)
        self.assertGreater(rep["all"]["gain"], 0)
        self.assertGreater(rep["all"]["z"], 3.0)
        self.assertGreater(rep["all"]["apart"]["games"], 50)                           # many games 5+ points apart
        self.assertGreater(rep["all"]["apart"]["per100"], 0)                           # following the books paid

    def test_leagues_are_split_and_small_ones_are_left_out(self):
        for _ in range(60):
            self.game("KXNBAGAME", 0.5, 0.03, 0.03)
        for _ in range(40):
            self.game("KXNHLGAME", 0.5, 0.03, 0.03)
        for _ in range(10):
            self.game("KXEPLGAME", 0.5, 0.03, 0.03)
        rep = sports_study.report(self.store)
        self.assertEqual({r["league"] for r in rep["leagues"]}, {"NBA", "NHL"})
        self.assertEqual(rep["leagues"][0]["league"], "NBA")                           # biggest first
        self.assertGreater(rep["needed_z"], 1.5)                                       # the bar rises with leagues compared

    def test_a_market_with_only_one_signal_is_ignored(self):
        self.store.x("INSERT INTO signal_grades(ts, bet_id, signal, prob, outcome, brier, clv_direction, segment) "
                     "VALUES(1,NULL,'kalshi_market',0.5,1,0.25,NULL,'KXNBAGAME-26OCT30G1-A')")
        self.assertEqual(sports_study.report(self.store)["games"], 0)


if __name__ == "__main__":
    unittest.main()
