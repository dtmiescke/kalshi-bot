"""A slow or dropped upload must not cost a 3-hour stream its memory, and a failing save must say why."""
import asyncio
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PUSH = str(ROOT / ".github" / "push_with_retry.sh")


def git(*args, cwd, **kw):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, **kw)


class TestPushRetry(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.remote = os.path.join(self.d.name, "remote.git")
        self.work = os.path.join(self.d.name, "work")
        os.makedirs(self.work)
        git("init", "-q", "--bare", self.remote, cwd=self.d.name)
        git("init", "-q", cwd=self.work)
        git("config", "user.email", "t@t", cwd=self.work)
        git("config", "user.name", "t", cwd=self.work)
        Path(self.work, "f").write_text("x")
        git("add", "-A", cwd=self.work)
        git("commit", "-qm", "c", cwd=self.work)

    def tearDown(self):
        self.d.cleanup()

    def hook(self, body):
        p = Path(self.remote, "hooks", "pre-receive")
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def push(self, **env):
        e = {**os.environ, "SAVE_PUSH_BACKOFF": "0", **env}
        return subprocess.run(["bash", PUSH, self.remote, "bot-data"], cwd=self.work, env=e,
                              capture_output=True, text=True)

    def test_a_push_that_fails_once_is_retried_and_lands(self):
        flag = os.path.join(self.d.name, "tried")
        self.hook(f'if [ ! -f "{flag}" ]; then touch "{flag}"; exit 1; fi')
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("push attempt 1 of 3 failed", r.stderr)
        self.assertTrue(git("rev-parse", "refs/heads/bot-data", cwd=self.remote).stdout.strip())

    def test_it_gives_up_after_the_last_attempt(self):
        self.hook("exit 1")
        r = self.push(SAVE_PUSH_ATTEMPTS="2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("push attempt 2 of 2 failed", r.stderr)

    def test_save_script_uses_it(self):
        script = (ROOT / ".github" / "save.sh").read_text(encoding="utf-8")
        self.assertIn("push_with_retry.sh", script)
        self.assertNotIn("git push", script)


class TestSaveReporting(unittest.TestCase):
    def test_a_failing_save_says_why_and_alerts_at_the_first_and_third_failure_only(self):
        from bot.config import Settings
        from bot.live import runner as rn
        from bot.storage import Store
        from tests.test_live import FakeRest

        async def idle_feed(*a, **k):
            await asyncio.sleep(3600)

        env = {"SAVE_CMD": "sh -c 'echo remote disk full >&2; exit 1'", "SAVE_EVERY_SECONDS": "1"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**env, "BOT_DB": os.path.join(d, "b.db")}):
            s = Settings()
            r = rn.LiveRunner(s, Store(s.db_path), rest=FakeRest(time.time() + 600), llm=None,
                              seed=False, with_context=False)
            sent = []
            with mock.patch.object(rn.feeds, "run_feed", idle_feed), \
                 mock.patch.object(rn.alerts, "send", lambda *a, **k: sent.append(a)), \
                 self.assertLogs("bot", level="ERROR") as logs:
                asyncio.run(r.main(0.12))                              # about seven seconds
            self.assertTrue(any("remote disk full" in line for line in logs.output), logs.output)
            self.assertTrue(any("in a row" in line for line in logs.output))
            self.assertEqual(len(sent), 2)                              # first failure and the third
            self.assertIn("remote disk full", sent[0][1])


if __name__ == "__main__":
    unittest.main()
