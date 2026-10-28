"""The live stream must run across every window boundary, so the pause between bursts falls mid-window."""
import unittest

from bot.main import aligned_burst_minutes

T = 1_793_187_000 - (1_793_187_000 % 900)       # a quarter-hour mark


def end_phase(now, env=None):
    m = aligned_burst_minutes(now, 8.0, env or {})
    return (now + m * 60 - T) % 900, m


class TestBurstAlignment(unittest.TestCase):
    def test_bursts_end_four_minutes_into_a_window(self):
        for start in (T + 400, T + 560, T + 700, T + 850):       # 6.7 to 14 minutes into a window
            phase, m = end_phase(start)
            self.assertAlmostEqual(phase, 240, delta=0.5)
            self.assertGreaterEqual(m, 4.0)
            self.assertLessEqual(m, 13.0)

    def test_a_late_start_runs_to_the_next_stop_not_a_short_burst(self):
        phase, m = end_phase(T + 100)                            # only 2.3 min to +4: skip to the next one
        self.assertAlmostEqual(phase, 240, delta=0.5)

    def test_every_boundary_is_streamed_through(self):
        # a burst starting anywhere from +5 to +14 minutes covers the next boundary and its first 90 seconds
        for off in range(300, 840, 30):
            start = T + off
            end = start + aligned_burst_minutes(start, 8.0, {}) * 60
            nxt = T + 900
            self.assertGreaterEqual(end, nxt + 90, off)

    def test_a_long_stream_ends_four_minutes_into_a_window_too(self):
        env = {"BURST_TARGET_MINUTES": "180"}
        for start in (T + 400, T + 700, T + 5000):
            end = start + aligned_burst_minutes(start, 8.0, env) * 60
            self.assertAlmostEqual((end - T) % 900, 240, delta=0.5)
            self.assertLessEqual(end - start, 180 * 60)
            self.assertGreater(end - start, 175 * 60 - 900)         # as close to 3 hours as the stop point allows

    def test_can_be_turned_off(self):
        self.assertEqual(aligned_burst_minutes(T + 400, 8.0, {"BURST_ALIGN": "no"}), 8.0)
        self.assertEqual(aligned_burst_minutes(T + 400, 8.0, {"BURST_END_PHASE_SECONDS": "x"}), 8.0)


if __name__ == "__main__":
    unittest.main()
