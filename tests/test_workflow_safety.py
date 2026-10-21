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

    def _seed_remote(self, root):
        bare = root / "remote.git"
        seed = root / "seed"
        bare.mkdir()
        self._git(root, "init", "--bare", "-q", str(bare))
        seed.mkdir()
        self._git(seed, "init", "-q")
        self._git(seed, "config", "user.name", "test")
        self._git(seed, "config", "user.email", "test@example.invalid")
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        self._git(seed, "add", "README.md")
        self._git(seed, "commit", "-qm", "seed")
        self._git(seed, "branch", "-M", "main")
        self._git(seed, "remote", "add", "origin", str(bare))
        self._git(seed, "push", "-q", "origin", "main")
        return bare

    def test_load_refuses_to_continue_when_remote_cannot_be_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "work"
            cwd.mkdir()
            self._git(cwd, "init", "-q")
            self._git(cwd, "remote", "add", "origin", str(root / "missing-remote.git"))
            result = subprocess.run(
                ["bash", str(ROOT / ".github" / "load.sh")],
                cwd=cwd, capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Refusing to run", result.stderr)
            self.assertFalse((cwd / "data" / ".memory_restored").exists())

    def test_load_allows_bootstrap_only_when_remote_is_reachable_and_branch_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = self._seed_remote(root)
            cwd = root / "work"
            cwd.mkdir()
            self._git(cwd, "init", "-q")
            self._git(cwd, "remote", "add", "origin", str(bare))
            result = subprocess.run(
                ["bash", str(ROOT / ".github" / "load.sh")],
                cwd=cwd, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("first-run initialization", result.stdout)
            self.assertTrue((cwd / "data" / ".memory_restored").exists())
            self.assertFalse((cwd / "data" / "bot.sqlite").exists())

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
