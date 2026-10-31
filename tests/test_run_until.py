import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".github", "run_until.sh")


def decide(until, now, dry=True):
    env = {**os.environ, "RUN_UNTIL": until, "NOW": now, "DRY_RUN": "1" if dry else "0"}
    out = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=30)
    return out.returncode, out.stdout.strip()


class TestRunUntil(unittest.TestCase):
    def test_before_the_stop_date_it_goes(self):
        code, text = decide("2026-11-02T08:00:00Z", "2026-11-01T23:59:59Z")
        self.assertEqual((code, text.split()[0]), (0, "go"))

    def test_at_or_after_the_stop_date_it_stops_and_only_prints_when_dry(self):
        for now in ("2026-11-02T08:00:00Z", "2026-11-03T00:00:00Z", "2027-01-01T00:00:00Z"):
            code, text = decide("2026-11-02T08:00:00Z", now)
            self.assertEqual((code, text.split()[0]), (0, "stop"), now)

    def test_no_stop_date_never_stops(self):
        for until in ("", "never"):
            code, text = decide(until, "2030-01-01T00:00:00Z")
            self.assertEqual((code, text.split()[0]), (0, "go"), until)

    def test_the_workflow_runs_the_guard_first_with_a_default_and_a_variable(self):
        text = open(os.path.join(ROOT, ".github", "workflows", "bot.yml")).read()
        self.assertIn("vars.APP_RUN_UNTIL || 'never'", text)                                  # no stop date unless one is set
        self.assertIn("run: bash .github/run_until.sh", text)
        self.assertLess(text.index("bash .github/run_until.sh"), text.index("pip install"))        # before any work
        self.assertLess(text.index("bash .github/run_until.sh"), text.index("python -m bot.main scheduled"))


if __name__ == "__main__":
    unittest.main()
