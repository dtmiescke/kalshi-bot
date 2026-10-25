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


    def test_dashboard_reads_compressed_memory_and_legacy_snapshot(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('x.name==="bot.sqlite.gz"', html)
        self.assertIn('x.name==="bot.sqlite"', html)
        self.assertIn("new DecompressionStream(\"gzip\")", html)

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

    # --- stale-base guard: never overwrite memory another job saved meanwhile --------
    def _memory_remote(self, root):
        """Bare remote with main + a bot-data branch holding a valid gzipped DB."""
        import gzip
        import sqlite3
        bare = self._seed_remote(root)
        seed = root / "seed"
        self._git(seed, "checkout", "-q", "--orphan", "bot-data")
        self._git(seed, "rm", "-rfq", ".")
        db = root / "seed.sqlite"
        sqlite3.connect(db).execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)").connection.commit()
        (seed / "bot.sqlite.gz").write_bytes(gzip.compress(db.read_bytes()))
        self._git(seed, "add", "bot.sqlite.gz")
        self._git(seed, "commit", "-qm", "memory")
        self._git(seed, "push", "-q", "origin", "bot-data")
        return bare, seed

    def _job(self, root, bare, name):
        cwd = root / name
        cwd.mkdir()
        self._git(cwd, "init", "-q")
        self._git(cwd, "remote", "add", "origin", str(bare))
        env = dict(os.environ, MEMORY_REMOTE=str(bare), PYTHONPATH=str(ROOT), NTFY_TOPIC="")
        load = subprocess.run(["bash", str(ROOT / ".github" / "load.sh")], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(load.returncode, 0, load.stdout + load.stderr)
        return cwd, env

    def _save(self, cwd, env):
        return subprocess.run(["bash", str(ROOT / ".github" / "save.sh")], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=120)

    def _remote_head(self, bare):
        return self._git(bare, "rev-parse", "bot-data").stdout.strip()

    def test_save_updates_memory_and_allows_a_second_save_in_the_same_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, _ = self._memory_remote(root)
            cwd, env = self._job(root, bare, "job")
            restored = (cwd / "data" / ".memory_base").read_text().strip()
            self.assertEqual(restored, self._remote_head(bare))
            first = self._save(cwd, env)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            saved = self._remote_head(bare)
            self.assertNotEqual(saved, restored)
            self.assertEqual((cwd / "data" / ".memory_base").read_text().strip(), saved)
            second = self._save(cwd, env)             # e.g. a mid-session save
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_save_refuses_when_another_job_saved_meanwhile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, seed = self._memory_remote(root)
            cwd, env = self._job(root, bare, "bot")
            # the trainer saves newer memory while the bot is still running
            (seed / "TRAINER.txt").write_text("newer memory\n")
            self._git(seed, "add", "TRAINER.txt")
            self._git(seed, "commit", "-qm", "trainer save")
            self._git(seed, "push", "-q", "origin", "bot-data")
            newer = self._remote_head(bare)
            result = self._save(cwd, env)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("refusing to overwrite newer memory", result.stderr)
            self.assertEqual(self._remote_head(bare), newer)      # newer copy kept

    def test_save_refuses_without_a_recorded_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, _ = self._memory_remote(root)
            cwd, env = self._job(root, bare, "job")
            (cwd / "data" / ".memory_base").unlink()
            before = self._remote_head(bare)
            result = self._save(cwd, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self._remote_head(bare), before)


if __name__ == "__main__":
    unittest.main()
