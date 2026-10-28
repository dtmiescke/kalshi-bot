"""Stats reset (STATS_SINCE): the account's P/L, balance, loss limits, bets.csv
and report restart, while every older bet stays in the database and keeps
feeding learning."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from bot import coin_learning, dashboard, guard, report
from bot.storage import Store, stats_since

RESET = "2026-10-28T11:45:00Z"
RESET_TS = 1793187900  # 2026-10-28T11:45:00Z


class TestStatsSince(unittest.TestCase):
    def test_parses_iso_and_epoch_and_fails_safe(self):
        self.assertEqual(stats_since(RESET), RESET_TS)
        self.assertEqual(stats_since(str(RESET_TS)), RESET_TS)
        self.assertEqual(stats_since(""), 0)
        self.assertEqual(stats_since("not a time"), 0)    # bad value = all history, never skips limits
        with patch.dict(os.environ, {"STATS_SINCE": RESET}):
            self.assertEqual(stats_since(), RESET_TS)


class TestStatsReset(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        now = int(time.time())
        self.since = now - 3600
        old = self.since - 86400
        bets = [  # before the reset: a $600 loss that would trip a $500 stop, and an open bet
            (old, "KXBTC15M-OLD1", "graded", "no", -600.0, 100.0),
            (old, "KXETH15M-OLD2", "open", None, None, 50.0),
            # after the reset: one $5 win and one $10 open bet
            (now - 60, "KXSOL15M-NEW1", "graded", "yes", 5.0, 5.0),
            (now - 30, "KXBNB15M-NEW2", "open", None, None, 10.0),
        ]
        for ts, ticker, status, result, pnl, stake in bets:
            self.st.x("""INSERT INTO bets(ts,mode,ticker,event_ticker,series,side,price,contracts,stake,fee,
                         status,result,pnl,graded_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (ts, "paper", ticker, ticker, ticker.split("-")[0], "yes", 0.5, int(stake * 2),
                       stake, 0, status, result, pnl, ts + 900 if status == "graded" else None))
        self.env = patch.dict(os.environ, {"STATS_SINCE": str(self.since), "MAX_TOTAL_LOSS": "500",
                                           "MAX_DRAWDOWN": "100", "BOT_MODE": "paper"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.st.db.close()
        self.d.cleanup()

    def test_account_balance_restarts(self):
        acct = self.st.account(500, mode="paper")
        self.assertEqual(acct["realized"], 5.0)
        self.assertEqual(acct["open_positions"], 1)
        self.assertEqual(acct["open_cost"], 10.0)
        self.assertEqual(acct["cash"], 495.0)          # 500 + 5 won - 10 in the open bet

    def test_loss_limits_ignore_bets_before_the_reset(self):
        self.assertFalse(guard.total_loss_tripped(self.st, mode="paper"))
        self.assertEqual(guard.status(self.st, 500, mode="paper"), (True, "ok"))
        with patch.dict(os.environ, {"STATS_SINCE": ""}):      # without the reset it would stop
            self.assertTrue(guard.total_loss_tripped(self.st, mode="paper"))

    def test_app_csv_and_report_start_fresh(self):
        csv_text = dashboard.export_csv(self.st, mode="paper")
        self.assertIn("KXSOL15M-NEW1", csv_text)
        self.assertNotIn("OLD", csv_text)
        text = report.build(self.st)
        self.assertIn("Bets: 2 total | 1 open | 1 graded", text)

    def test_learning_keeps_every_bet(self):
        self.assertEqual(self.st.q("SELECT COUNT(*) n FROM bets")[0]["n"], 4)   # nothing deleted
        coins = coin_learning.coin_report(self.st)
        self.assertEqual(coins["graded_bets"], 2)                               # old loss still teaches
        btc = next(c for c in coins["coins"] if c["coin"] == "BTC")
        self.assertEqual(btc["net_profit"], -600.0)

    def test_summary_tells_the_app_the_limits_and_reset(self):
        with patch.dict(os.environ, {"BANKROLL": "500", "MIN_PER_BET": "5", "MAX_PER_BET": "20",
                                     "CRYPTO_MIN_BET": "5", "CRYPTO_MAX_PER_BET": "20"}):
            acc = dashboard.summary(self.st)["account"]
        self.assertEqual(acc["bankroll"], 500.0)
        self.assertEqual(acc["stats_since"], self.since)
        self.assertEqual((acc["min_per_bet"], acc["max_per_bet"]), (5.0, 20.0))


class TestWorkflowsAgree(unittest.TestCase):
    def test_every_memory_saving_workflow_uses_the_same_reset_and_bankroll(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        for name in ("bot.yml", "clear-pause.yml"):      # train.yml no longer saves memory
            text = (root / name).read_text()
            self.assertIn("STATS_SINCE: ${{ vars.APP_STATS_SINCE || '%s' }}" % RESET, text, name)
            self.assertIn("BANKROLL: ${{ vars.APP_BANKROLL || '500' }}", text, name)


if __name__ == "__main__":
    unittest.main()
