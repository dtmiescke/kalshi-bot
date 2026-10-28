"""The settings sweep: an in-sample winner must survive unseen windows and the number of tries."""
import math
import random
import unittest

from bot import sweep


def rows(n, seed, edge=0.0):
    """Windows where the formula's probability is the truth shifted by `edge` from Kalshi's price."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        k = rng.uniform(0.3, 0.7)
        true_p = min(max(k + edge * rng.choice((-1, 1)), 0.02), 0.98)
        sign = 1 if true_p > k else -1
        p = min(max(k + sign * abs(edge or 0.1), 0.02), 0.98)
        y = int(rng.random() < (true_p if edge else k))
        out.append((1000 + i, p, k, 0.02, y))
    return out


class TestSweep(unittest.TestCase):
    def test_noise_does_not_hold_up(self):
        tried, best, need, verdict = sweep.run(rows(1500, seed=1, edge=0.0))
        self.assertEqual(len(tried), len(sweep.EDGE_BARS) * len(sweep.SHRINKS))
        self.assertNotEqual(verdict, "holds up")

    def test_a_real_edge_holds_up(self):
        tried, best, need, verdict = sweep.run(rows(4000, seed=2, edge=0.15))
        self.assertEqual(verdict, "holds up", best)
        self.assertGreater(best["test_mean"], 0)

    def test_too_few_windows_says_so(self):
        self.assertEqual(sweep.run(rows(20, seed=3))[3], "too few windows")

    def test_more_tries_raise_the_bar(self):
        self.assertGreater(sweep.run(rows(100, seed=4))[2], 1.64)

    def test_empty_store_is_safe(self):
        out = sweep.run([])
        self.assertEqual(out[3], "too few windows")


if __name__ == "__main__":
    unittest.main()
