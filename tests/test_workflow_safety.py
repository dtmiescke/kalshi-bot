"""Offline integration tests for the GitHub Actions persistence safeguards."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestWorkflowSafety(unittest.TestCase):
    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )

    def test_load_refuses_to_continue_when_memory_branch_cannot_be_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._git(cwd, "init", "-q")
            self._git(cwd, "remote", "add", "origin", (cwd / "missing-remote.git").as_uri())
            result = subprocess.run(
                ["bash", str(ROOT / ".github" / "load.sh")],
                cwd=cwd, capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("refusing to run", result.stderr)
            self.assertFalse((cwd / "data" / ".memory_restored").exists())

    def test_save_refuses_to_publish_without_verified_restore_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            env = dict(os.environ)
            env.update({"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo"})
            result = subprocess.run(
                ["bash", str(ROOT / ".github" / "save.sh")],
                cwd=cwd, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("was not verified", result.stderr)


if __name__ == "__main__":
    unittest.main()
