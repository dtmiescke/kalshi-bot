"""Self-queued bot cycles: GitHub's cron skips most runs, so each saved cycle
queues the next one on main without starving the trainer or overriding a pause."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "chain_next.sh"

FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "api repos/"*) echo "$FAKE_STATE" ;;
  "run list") echo "$FAKE_WAITING" ;;
  "workflow run") exit 0 ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
"""


class TestChainScript(unittest.TestCase):
    def run_script(self, state="active", waiting="0", *args):
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp) / "gh"
            gh.write_text(FAKE_GH, encoding="utf-8")
            gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
            log = Path(tmp) / "gh.log"
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GH_LOG=str(log),
                       FAKE_STATE=state, FAKE_WAITING=waiting,
                       GITHUB_REPOSITORY="owner/kalshi-bot")
            out = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True,
                                 text=True, check=True)
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            return out.stdout, calls

    def dispatched(self, calls):
        return [c for c in calls if c.startswith("workflow run")]

    def test_queues_next_run_on_main(self):
        out, calls = self.run_script()
        self.assertEqual(self.dispatched(calls), ["workflow run bot.yml -R owner/kalshi-bot --ref main"])
        self.assertIn("queued", out)

    def test_paused_bot_stays_paused(self):
        out, calls = self.run_script("disabled_manually")
        self.assertEqual(self.dispatched(calls), [])
        self.assertIn("disabled_manually", out)

    def test_waiting_trainer_goes_first(self):
        out, calls = self.run_script("active", "1")
        self.assertEqual(self.dispatched(calls), [])
        self.assertIn("Trainer is waiting", out)

    def test_trainer_restarts_chain_even_if_another_is_queued(self):
        _, calls = self.run_script("active", "1", "after-training")
        self.assertEqual(len(self.dispatched(calls)), 1)
        self.assertFalse(any(c.startswith("run list") for c in calls))

    def test_after_training_still_respects_pause(self):
        _, calls = self.run_script("disabled_manually", "0", "after-training")
        self.assertEqual(self.dispatched(calls), [])


def _step(text, name):
    """Return the text of one workflow step (from its '- name:' line to the next)."""
    marker = f"      - name: {name}\n"
    start = text.index(marker)
    nxt = text.find("\n      - name: ", start + len(marker))
    return text[start:] if nxt == -1 else text[start:nxt]


class TestWorkflowWiring(unittest.TestCase):
    """Text checks (no YAML dependency in requirements.txt)."""

    def load(self, name):
        return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def test_bot_chains_only_after_a_saved_cycle_on_main(self):
        wf = self.load("bot.yml")
        self.assertIn("  actions: write", wf)
        self.assertIn("id: save-memory", _step(wf, "Save memory"))
        chain = _step(wf, "Queue the next run")
        for part in ("steps.save-memory.outcome == 'success'", "refs/heads/main",
                     "vars.CHAIN_RUNS != 'no'", "run: bash .github/chain_next.sh"):
            self.assertIn(part, chain)
        self.assertTrue(wf.rstrip().endswith("run: bash .github/chain_next.sh"))  # last step
        # the cron fallback and the shared concurrency group stay in place
        self.assertIn('cron: "*/12 * * * *"', wf)
        self.assertIn("group: kalshi-bot", wf)
        self.assertIn("group: kalshi-bot", self.load("train.yml"))

    def test_trainer_restarts_chain_when_done(self):
        wf = self.load("train.yml")
        self.assertIn("  actions: write", wf)
        step = _step(wf, "Restart the bot's run chain")
        self.assertIn("if: always()", step)
        self.assertIn("run: bash .github/chain_next.sh after-training", step)

    def test_trainer_runs_daily_and_audits_without_blocking_save(self):
        wf = self.load("train.yml")
        self.assertIn('cron: "7 9 * * *"', wf)
        self.assertIn("cancel-in-progress: false", wf)
        steps = (
            ("Backtest (read-only audit)", "bot.main backtest"),
            ("Contract rules and MCP-adapter regression tests", "tests.test_market_rules"),
            ("Full regression suite", "unittest discover"),
            ("Report (read-only audit)", "bot.main report"),
        )
        for name, command in steps:
            step = _step(wf, name)
            self.assertIn("continue-on-error: true", step)
            self.assertIn(command, step)
            self.assertLess(wf.index(name), wf.index("Save memory"))
        # Keep all audits independent: one failure must not skip later checks or persistence.
        positions = [wf.index(name) for name, _ in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("tests.test_contract_pdf", _step(wf, "Contract rules and MCP-adapter regression tests"))
        self.assertIn("tests.test_kalshi_series_metadata", _step(wf, "Contract rules and MCP-adapter regression tests"))
        self.assertIn("tests.test_snapshot_contract_rules", _step(wf, "Contract rules and MCP-adapter regression tests"))

    def test_paper_mode_is_hard_locked(self):
        run_step = _step(self.load("bot.yml"), "Run")
        self.assertIn("BOT_MODE: paper", run_step)
        self.assertIn('LIVE_TRADING_CONFIRMED: "no"', run_step))


if __name__ == "__main__":
    unittest.main()
