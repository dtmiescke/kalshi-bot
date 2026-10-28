"""Optional Hugging Face memory: off without HF_TOKEN; with it, load/save use a
private HF dataset repo (no GitHub 100 MB limit) with the same fail-closed and
refuse-if-moved rules as bot-data. The scripts are exercised end to end against
a local fake of huggingface_hub (tests/fake_hf)."""
import gzip
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_hf"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class TestModule(unittest.TestCase):
    def test_off_without_token(self):
        from bot import hf_memory
        with mock.patch.dict(os.environ, {"HF_TOKEN": "", "HF_MEMORY_REPO": "u/m"}):
            self.assertFalse(hf_memory.enabled())
            self.assertEqual(hf_memory.main(["enabled"]), 1)
            self.assertEqual(hf_memory.main(["load", "x"]), 2)
        with mock.patch.dict(os.environ, {"HF_TOKEN": "t", "HF_MEMORY_REPO": "u/m"}):
            self.assertTrue(hf_memory.enabled())


class TestScripts(unittest.TestCase):
    """load.sh / save.sh with HF configured (fake HF backend on disk)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "hf").mkdir()
        self.bare = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(self.bare))
        seed = self.root / "seed"
        seed.mkdir()
        git(seed, "init", "-q")
        git(seed, "config", "user.email", "t@t")
        git(seed, "config", "user.name", "t")
        git(seed, "remote", "add", "origin", str(self.bare))
        git(seed, "checkout", "-q", "--orphan", "bot-data")
        db = self.root / "seed.sqlite"
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
            c.execute("INSERT INTO kv VALUES('origin','bot-data')")
        (seed / "bot.sqlite.gz").write_bytes(gzip.compress(db.read_bytes()))
        git(seed, "add", "bot.sqlite.gz")
        git(seed, "commit", "-qm", "memory")
        git(seed, "push", "-q", "origin", "bot-data")

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = dict(os.environ, MEMORY_REMOTE=str(self.bare), NTFY_TOPIC="",
                   PYTHONPATH=f"{FAKE}{os.pathsep}{ROOT}", HF_TOKEN="t",
                   HF_MEMORY_REPO="owner/kalshi-memory", FAKE_HF_DIR=str(self.root / "hf"))
        env.update(extra)
        return env

    def job(self, name, **extra):
        cwd = self.root / name
        cwd.mkdir()
        git(cwd, "init", "-q")
        git(cwd, "remote", "add", "origin", str(self.bare))
        env = self.env(**extra)
        r = subprocess.run(["bash", str(ROOT / ".github" / "load.sh")], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=60)
        return cwd, env, r

    def save(self, cwd, env):
        return subprocess.run(["bash", str(ROOT / ".github" / "save.sh")], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=120)

    def kv(self, cwd, k):
        with sqlite3.connect(cwd / "data" / "bot.sqlite") as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row and row[0]

    def test_migrates_then_round_trips_through_hf(self):
        cwd, env, r = self.job("first")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("migrating from bot-data", r.stdout)
        self.assertEqual(self.kv(cwd, "origin"), "bot-data")
        with sqlite3.connect(cwd / "data" / "bot.sqlite") as c:
            c.execute("INSERT INTO kv VALUES('probe','saved-to-hf')")
        s = self.save(cwd, env)
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertIn("saved to Hugging Face", s.stdout)
        hf_files = list((self.root / "hf").rglob("bot.sqlite.gz"))
        self.assertEqual(len(hf_files), 1)
        # bot-data now carries only the small files; the database lives on HF
        listing = git(self.root, "--git-dir", str(self.bare), "ls-tree", "--name-only",
                      "bot-data").stdout.split()
        self.assertNotIn("bot.sqlite.gz", listing)
        self.assertIn("REPORT.txt", listing)

        cwd2, env2, r2 = self.job("second")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("restored from Hugging Face", r2.stdout)
        self.assertEqual(self.kv(cwd2, "probe"), "saved-to-hf")
        again = self.save(cwd2, env2)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_save_refuses_when_hf_memory_moved(self):
        cwd, env, _ = self.job("bot")
        self.assertEqual(self.save(cwd, env).returncode, 0)       # migrate to HF
        cwd2, env2, _ = self.job("trainer")                       # restores the same commit
        with sqlite3.connect(cwd / "data" / "bot.sqlite") as c:
            c.execute("INSERT INTO kv VALUES('newer','bot')")
        self.assertEqual(self.save(cwd, env).returncode, 0)       # bot saves first
        late = self.save(cwd2, env2)                              # trainer's base is stale
        self.assertNotEqual(late.returncode, 0)
        self.assertIn("not saved", late.stderr)
        cwd3, _, _ = self.job("check")
        self.assertEqual(self.kv(cwd3, "newer"), "bot")           # newer memory kept

    def test_hf_outage_stops_the_run(self):
        cwd, env, r = self.job("down", FAKE_HF_FAIL="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to run", r.stderr)
        self.assertFalse((cwd / "data" / ".memory_restored").exists())

    def test_unconfigured_uses_bot_data_as_before(self):
        cwd, env, r = self.job("plain", HF_TOKEN="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Hugging Face", r.stdout)
        s = self.save(cwd, env)
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        listing = git(self.root, "--git-dir", str(self.bare), "ls-tree", "--name-only",
                      "bot-data").stdout.split()
        self.assertIn("bot.sqlite.gz", listing)
        self.assertFalse(list((self.root / "hf").rglob("bot.sqlite.gz")))


if __name__ == "__main__":
    unittest.main()
