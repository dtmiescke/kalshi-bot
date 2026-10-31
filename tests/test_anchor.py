import unittest

from bot import anchor
import tests.test_nfl_tonight as nt
from tests.test_nfl_tonight import NOW, log, mk


class TestRules(unittest.TestCase):
    def test_season_average_needs_enough_games_else_uses_his_recent_ones(self):
        self.assertEqual(anchor.season_average([10.0] * 5, [99.0] * 40), 10.0)
        self.assertEqual(anchor.season_average([10.0] * 3, [20.0] * 40), 20.0)
        self.assertIsNone(anchor.season_average([], []))

    def test_the_projection_is_held_near_his_average(self):
        self.assertEqual(anchor.clamp(30.0, 20.0), (22.0, True))                 # at most 10% above
        self.assertEqual(anchor.clamp(10.0, 20.0), (16.0, True))                 # at most 20% below
        mu, held = anchor.clamp(21.0, 20.0)
        self.assertEqual((mu, held), (21.0, False))
        self.assertEqual(anchor.clamp(30.0, None), (30.0, False))
        self.assertEqual(anchor.clamp(30.0, 0.0), (30.0, False))

    def test_overs_must_be_on_lines_he_can_reach(self):
        self.assertTrue(anchor.reachable(23, 20.0))                              # 15% above
        self.assertFalse(anchor.reachable(24, 20.0))
        self.assertTrue(anchor.reachable(1, 0.8))                                # small counts get a whole unit of room
        self.assertFalse(anchor.reachable(3, 0.8))
        self.assertTrue(anchor.reachable(99, None))

    def test_probability_blends_his_record_with_a_widened_curve_and_is_clipped(self):
        xs = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0]
        mid = anchor.blend_prob(xs, 17.0)
        self.assertGreater(mid, 0.3)
        self.assertLess(mid, 0.7)
        self.assertGreater(anchor.blend_prob(xs, 5.0), anchor.blend_prob(xs, 17.0))
        self.assertLessEqual(anchor.blend_prob(xs, 500.0), 0.97)                  # never claims certainty
        self.assertGreaterEqual(anchor.blend_prob(xs, -500.0), 0.03)
        self.assertIsNone(anchor.blend_prob([1.0, 2.0], 1.5))
        flat = anchor.blend_prob([5.0] * 6, 4.0)                                  # no spread at all: his record only, still inside the clip
        self.assertTrue(0.9 < flat <= 0.97)


class TestOnTheNflTracker(nt.TestReport):
    def test_an_over_far_above_his_average_is_not_a_pick_even_when_it_is_cheap(self):
        reach = [mk("T-130", "Star Receiver", 129.5, .02, .06)]                  # he averages ~90: 130+ is way out of reach
        rep = __import__("bot.nfl_tonight", fromlist=["x"]).build_report(self.world(reach), self.store, NOW)
        self.assertEqual(rep["picks"], [])

    def test_a_hot_streak_cannot_lift_the_projection_far_above_his_average(self):
        from bot import nfl_tonight as nfl
        steady = [log(f"2026-09-{d:02d}", receivingYards=80.0) for d in range(1, 11)]
        surge = steady + [log(f"2026-09-{d:02d}", receivingYards=200.0) for d in range(11, 14)]
        mu, _ = nfl.projection(surge, "receivingYards")
        avg = sum(x["stats"]["receivingYards"] for x in surge) / len(surge)
        from bot import anchor as a
        held, was_held = a.clamp(mu, 80.0)
        self.assertLessEqual(held, 80.0 * 1.10 + 1e-9)
        self.assertTrue(was_held)
        self.assertGreater(mu, held)                                             # the raw projection really was above the cap


if __name__ == "__main__":
    unittest.main()
