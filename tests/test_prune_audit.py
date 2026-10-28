"""decision_audit keeps every scored (canonical) forecast for calibration and
backtests; unscored repeat scans go a day after settling and never-settled
rows after 14 days."""
import os
import tempfile
import unittest

from bot import brain, maintenance
from bot.storage import Store

NOW = 1_800_000_000
DAY = 86400


class TestPruneAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.tmp.name, "t.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, ticker, age_days, prob=0.6):
        self.st.add_decision_audit({"ts": NOW - int(age_days * DAY), "ticker": ticker, "series": "KXBTCD",
                                    "decision": "rejected", "model_prob": prob, "market_price": 0.5})

    def test_unscored_repeats_and_stale_unsettled_rows_go_scored_rows_stay(self):
        for age in (5, 4.9, 4.8):                      # three scans of one old market
            self.scan("OLD", age)
        self.st.grade_decision_audit("OLD", 1, settled_ts=NOW - 4 * DAY, cutoff_ts=NOW - 4.85 * DAY)
        for age in (0.5, 0.4):                          # recent market, settled hours ago
            self.scan("NEW", age)
        self.st.grade_decision_audit("NEW", 0, settled_ts=NOW - 0.2 * DAY, cutoff_ts=NOW - 0.45 * DAY)
        self.scan("LOST", 20)                           # never graded
        self.scan("OPEN", 0.1)                          # not settled yet
        before = brain.calibration_table(self.st) if hasattr(brain, "calibration_table") else None

        res = maintenance.janitor_prune_audit(self.st, now=NOW)

        self.assertEqual(res["unscored"], 2)            # OLD's two repeats
        self.assertEqual(res["unsettled"], 1)           # LOST
        left = self.st.q("SELECT ticker, brier IS NOT NULL AS scored FROM decision_audit ORDER BY ts")
        self.assertEqual([(r["ticker"], r["scored"]) for r in left],
                         [("OLD", 1), ("NEW", 1), ("NEW", 0), ("OPEN", 0)])
        if before is not None:
            self.assertEqual(brain.calibration_table(self.st), before)   # calibration unchanged

    def test_run_janitors_reports_it(self):
        self.scan("X", 30)
        res = maintenance.run_janitors(self.st, now=NOW, force=True)
        self.assertEqual(res["janitor_d"]["unsettled"], 1)


if __name__ == "__main__":
    unittest.main()
