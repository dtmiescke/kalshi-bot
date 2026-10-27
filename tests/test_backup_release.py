"""Weekly memory backup to GitHub Releases: uploads the restored database,
keeps the newest BACKUP_KEEP memory-* releases, never touches anything else."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "backup_release.sh"

FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "release view") [ -n "$FAKE_EXISTS" ] && exit 0 || exit 1 ;;
  "release list") printf '%s\\n' $FAKE_TAGS ;;
  "release create"|"release upload"|"release delete") exit 0 ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
"""


class TestBackupScript(unittest.TestCase):
    def run_script(self, tags="", exists="", db_bytes=b"SQLite data", keep="4"):
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp) / "gh"
            gh.write_text(FAKE_GH, encoding="utf-8")
            gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
            db = Path(tmp) / "bot.sqlite"
            if db_bytes:
                db.write_bytes(db_bytes)
            log = Path(tmp) / "gh.log"
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GH_LOG=str(log),
                       FAKE_TAGS=tags, FAKE_EXISTS=exists, BACKUP_KEEP=keep,
                       GITHUB_REPOSITORY="owner/kalshi-bot")
            r = subprocess.run(["bash", str(SCRIPT), str(db)], env=env, capture_output=True, text=True)
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            return r, calls

    def test_creates_prerelease_with_the_memory(self):
        r, calls = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        create = [c for c in calls if c.startswith("release create memory-")]
        self.assertEqual(len(create), 1)
        self.assertIn(".sqlite.gz -R owner/kalshi-bot --prerelease", create[0])
        self.assertFalse([c for c in calls if c.startswith("release delete")])

    def test_same_day_rerun_replaces_the_asset(self):
        _, calls = self.run_script(exists="1")
        self.assertTrue(any(c.startswith("release upload") and "--clobber" in c for c in calls))
        self.assertFalse(any(c.startswith("release create") for c in calls))

    def test_keeps_only_the_newest_backups(self):
        tags = "memory-2026-10-25 memory-2026-10-18 memory-2026-10-11 memory-2026-10-04 memory-2026-09-27"
        _, calls = self.run_script(tags=tags, keep="4")
        deleted = [c for c in calls if c.startswith("release delete")]
        self.assertEqual(deleted, ["release delete memory-2026-09-27 -R owner/kalshi-bot --yes --cleanup-tag"])

    def test_missing_memory_fails_without_uploading(self):
        r, calls = self.run_script(db_bytes=b"")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(calls)


class TestBackupWorkflow(unittest.TestCase):
    def test_weekly_read_only_and_restores_first(self):
        wf = (ROOT / ".github" / "workflows" / "backup.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "23 4 * * 0"', wf)
        self.assertIn("contents: write", wf)
        self.assertNotIn("save.sh", wf)                 # never writes bot memory
        self.assertNotIn("group: kalshi-bot", wf)       # doesn't block the bot
        self.assertLess(wf.index("bash .github/load.sh"), wf.index("backup_release.sh"))
        self.assertIn("HF_TOKEN: ${{ secrets.HF_TOKEN }}", wf)


if __name__ == "__main__":
    unittest.main()
