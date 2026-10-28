"""Hand-over of the long stream: when it may end, and what makes it end early."""
import unittest

from bot import handoff

T = 1_793_187_000 - (1_793_187_000 % 900)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


def fake(ref_sha="aaa", pending=(), state="active"):
    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/workflows/bot.yml"):
            return Resp(200, {"state": state})
        if url.endswith("/git/ref/heads/main"):
            return Resp(200, {"object": {"sha": ref_sha}})
        return Resp(200, {"workflow_runs": list(pending)})
    return get


class TestStopPoints(unittest.TestCase):
    def test_next_stop_is_four_minutes_into_a_window_and_not_too_soon(self):
        self.assertEqual(handoff.next_stop(T + 100), T + 240)
        self.assertEqual(handoff.next_stop(T + 230), T + 1140)      # under 45 s away: the next window
        self.assertEqual(handoff.next_stop(T + 500), T + 1140)

    def test_stop_point_stays_inside_the_target(self):
        for off in (0, 123, 400, 899):
            end = handoff.stop_point(T + off, 3 * 3600)
            self.assertLessEqual(end, T + off + 3 * 3600)
            self.assertEqual((end - T) % 900, 240)


class TestRestartReason(unittest.TestCase):
    def test_nothing_to_hand_over_to(self):
        self.assertIsNone(handoff.restart_reason("aaa", "o/r", "tok", get=fake()))

    def test_the_pause_button_ends_the_stream(self):
        self.assertIn("paused", handoff.restart_reason("aaa", "o/r", "tok", get=fake(state="disabled_manually")))

    def test_a_deploy_ends_the_stream(self):
        self.assertIn("main moved", handoff.restart_reason("aaa", "o/r", "tok", get=fake(ref_sha="bbbbbbb")))

    def test_cron_fallback_runs_do_not_count_but_a_push_or_manual_run_does(self):
        cron = {"path": ".github/workflows/bot.yml", "event": "schedule", "name": "kalshi-bot"}
        push = {"path": ".github/workflows/bot.yml", "event": "push", "name": "kalshi-bot"}
        clear = {"path": ".github/workflows/clear-pause.yml", "event": "workflow_dispatch", "name": "clear"}
        other = {"path": ".github/workflows/train.yml", "event": "schedule", "name": "train"}
        self.assertIsNone(handoff.restart_reason("aaa", "o/r", "tok", get=fake(pending=[cron, other])))
        self.assertIn("push", handoff.restart_reason("aaa", "o/r", "tok", get=fake(pending=[cron, push])))
        self.assertIn("clear", handoff.restart_reason("aaa", "o/r", "tok", get=fake(pending=[clear])))

    def test_failures_keep_the_stream_running(self):
        def boom(*a, **k):
            raise OSError("network down")
        self.assertIsNone(handoff.restart_reason("aaa", "o/r", "tok", get=boom))
        self.assertIsNone(handoff.restart_reason("aaa", "o/r", None, get=fake(ref_sha="zzz")))   # no token


    def test_slow_background_work_never_stops_the_loop(self):
        import asyncio
        import os
        import tempfile
        import time
        from unittest import mock

        from bot.config import Settings
        from bot.live import runner as rn
        from bot.storage import Store
        from tests.test_live import FakeRest

        async def idle_feed(*a, **k):
            await asyncio.sleep(3600)

        env = {"GRADE_EVERY_SECONDS": "1", "SAVE_CMD": "sleep 4", "SAVE_EVERY_SECONDS": "1", "STREAM_DRAIN_SECONDS": "10"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**env, "BOT_DB": os.path.join(d, "b.db")}):
            s = Settings()
            r = rn.LiveRunner(s, Store(s.db_path), rest=FakeRest(time.time() + 600), llm=None,
                              seed=False, with_context=False)
            ticks = []
            slow = []

            def slow_grade(*a):
                slow.append(1)
                time.sleep(4)                      # a four-second grading pass
            with mock.patch.object(rn.feeds, "run_feed", idle_feed), \
                 mock.patch.object(rn, "grade", slow_grade), \
                 mock.patch.object(r, "tick", lambda now: ticks.append(now) or []), \
                 mock.patch.object(rn.alerts, "send", lambda *a, **k: None):
                asyncio.run(r.main(0.1, periodic=lambda: time.sleep(4), periodic_every=1))     # six seconds of streaming
            self.assertGreaterEqual(len(ticks), 5)     # it kept ticking while grading, saving and the periodic job ran
            self.assertGreaterEqual(len(slow), 1)


if __name__ == "__main__":
    unittest.main()


class TestStreamLoop(unittest.TestCase):
    def test_a_long_stream_grades_saves_and_hands_over_while_it_runs(self):
        import asyncio
        import os
        import tempfile
        import time
        from unittest import mock

        from bot.config import Settings
        from bot.live import runner as rn
        from bot.storage import Store
        from tests.test_live import FakeRest

        async def idle_feed(*a, **k):
            await asyncio.sleep(3600)

        env = {"GRADE_EVERY_SECONDS": "1", "HANDOFF_CHECK": "yes", "HANDOFF_EVERY_SECONDS": "1",
               "SAVE_CMD": "false", "SAVE_EVERY_SECONDS": "1", "GITHUB_SHA": "aaa",
               "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**env, "BOT_DB": os.path.join(d, "b.db")}):
            s = Settings()
            r = rn.LiveRunner(s, Store(s.db_path), rest=FakeRest(time.time() + 600), llm=None,
                              seed=False, with_context=False)
            graded, alerts_sent, periodic = [], [], []
            started = time.time()
            with mock.patch.object(rn.feeds, "run_feed", idle_feed), \
                 mock.patch.object(rn, "grade", lambda *a: graded.append(1)), \
                 mock.patch.object(rn.alerts, "send", lambda *a, **k: alerts_sent.append(a)), \
                 mock.patch.object(rn.handoff, "restart_reason", return_value="a deploy is waiting"), \
                 mock.patch.object(rn.handoff, "next_stop", lambda now, *a, **k: now + 5):
                asyncio.run(r.main(10, periodic=lambda: periodic.append(1), periodic_every=1))   # asked for ten minutes
            self.assertLess(time.time() - started, 60)         # handed over at the stop point instead
            self.assertGreaterEqual(len(graded), 3)            # settled while streaming (+1 at the end)
            self.assertGreaterEqual(len(periodic), 2)          # ladders and combos keep running through the stream
            self.assertEqual(len(alerts_sent), 2)              # a failing save alerts at the first and third failure, not every attempt
