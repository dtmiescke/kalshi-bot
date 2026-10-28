"""Storage trim: raw data stays small enough for GitHub; learned data is never trimmed."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from bot import sports_intel
from bot.maintenance import janitor_retention
from bot.storage import RAW_RETENTION, Store, trim_raw

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400


class TestStorageRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "bot.sqlite"))
        self.now = int(time.time())

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def _flow(self, trade_id, age_s):
        self.store.add_flow_trades([{"trade_id": trade_id, "ticker": "KXNBAGAME-X-BOS",
                                     "ts": self.now - age_s, "count": 1, "yes_price": .5,
                                     "taker_side": "yes"}])

    def _count(self, table):
        return self.store.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def test_flow_trades_kept_about_two_days_and_reads_still_work(self):
        self._flow("recent", 3 * 3600)          # inside the longest read window (3 h)
        self._flow("yesterday", DAY)
        self._flow("old", 3 * DAY)
        result = janitor_retention(self.store, now=self.now)
        self.assertEqual(result["before"]["flow_trades"], 3)
        self.assertEqual(result["after"]["flow_trades"], 2)
        ids = {r["trade_id"] for r in self.store.flow("KXNBAGAME-X-BOS", self.now - 4 * 3600)}
        self.assertEqual(ids, {"recent"})

    def test_crypto_15m_trades_kept_one_hour_others_two_days(self):
        for tid, ticker, age in (("c-new", "KXBTC15M-W1-00", 1800), ("c-old", "KXBTC15M-W0-00", 7200),
                                 ("s-2h", "KXNBAGAME-X-BOS", 7200), ("l-2h", "KXBTCD-26OCT2517-T80000", 7200)):
            self.store.add_flow_trades([{"trade_id": tid, "ticker": ticker, "ts": self.now - age,
                                         "count": 1, "yes_price": .5, "taker_side": "yes"}])
        result = janitor_retention(self.store, now=self.now)
        left = {r["trade_id"] for r in self.store.q("SELECT trade_id FROM flow_trades")}
        self.assertEqual(left, {"c-new", "s-2h", "l-2h"})
        self.assertEqual(result["before"]["flow_trades"] - result["after"]["flow_trades"], 1)
        # the crypto readers' windows (5 and 15 minutes) are still fully covered
        self.assertEqual(len(self.store.flow("KXBTC15M-W1-00", self.now - 3600)), 1)

    def test_crypto_readings_and_snapshots_kept_three_days_sports_longer(self):
        for ticker, age in (("KXBTC15M-W1-00", DAY), ("KXBNB15M-W0-00", 4 * DAY),
                            ("KXETHD-26OCT2517-T4000", 4 * DAY), ("KXNBAGAME-X-BOS", 4 * DAY)):
            self.store.add_signal(self.now - age, ticker, "kalshi_market", 0.5, 0.9, {})
            self.store.add_market_snapshot({"ts": self.now - age, "ticker": ticker,
                                            "event_ticker": "E", "series": "S", "title": "",
                                            "team": "", "yes_bid": .4, "yes_ask": .6,
                                            "last_price": .5, "volume": 0, "open_interest": 0,
                                            "close_time": None, "status": "active"})
        self.store.add_signal(self.now - 2 * DAY, "KXETHD-26OCT2617-T4000", "kalshi_market", .5, .9, {})
        self.store.add_signal(self.now - 2 * DAY, "KXETH15M-W2-00", "kalshi_market", .5, .9, {})
        janitor_retention(self.store, now=self.now)
        for table in ("signal_readings", "market_snapshots"):
            left = {r["ticker"] for r in self.store.q(f"SELECT ticker FROM {table}")}
            want = {"KXBTC15M-W1-00", "KXNBAGAME-X-BOS"}
            if table == "signal_readings":
                want.add("KXETH15M-W2-00")      # 15M readings keep 3 days, ladders 1
            self.assertEqual(left, want, table)

    def test_bnb_is_a_crypto_area(self):
        from bot import nerd
        self.assertEqual(nerd.health_area("KXBNB15M"), "crypto")
        self.assertEqual(nerd.health_area("KXBNBD"), "ladders")

    def test_sports_intel_items_trimmed_by_observed_time(self):
        sports_intel.ensure_schema(self.store)
        for i, age in enumerate((DAY, 20 * DAY)):
            self.store.x("INSERT INTO sports_intel_items (id, kind, category, source, title, "
                         "observed_ts, payload_json) VALUES (?,?,?,?,?,?,?)",
                         (f"n{i}", "news", "basketball", "rss", "t", self.now - age, "{}"))
        janitor_retention(self.store, now=self.now)
        self.assertEqual(self._count("sports_intel_items"), 1)

    def test_missing_optional_table_is_skipped(self):
        # sports_intel_items is created lazily; the janitor must not fail without it.
        result = janitor_retention(self.store, now=self.now)
        self.assertNotIn("sports_intel_items", result["before"])
        self.assertIn("flow_trades", result["after"])

    def test_compact_uses_the_same_windows_and_keeps_learning(self):
        self._flow("old", 3 * DAY)
        with self.store.lock:
            self.store.db.execute("INSERT INTO lessons (ts,model,text) VALUES (?,?,?)",
                                  (self.now - 400 * DAY, "m", "keep"))
            self.store.db.execute(
                "INSERT INTO bets (ts,mode,ticker,side,price,contracts,stake,fee) "
                "VALUES (?,?,?,?,?,?,?,?)", (self.now - 400 * DAY, "paper", "T", "yes", .5, 1, .5, 0))
            self.store.db.commit()
        self.store.compact()
        self.assertEqual(self._count("flow_trades"), 0)
        self.assertEqual(self._count("lessons"), 1)
        self.assertEqual(self._count("bets"), 1)

    def test_retention_only_names_raw_tables(self):
        learned = {"bets", "lessons", "weight_history", "graded_markets", "signal_grades",
                   "decision_audit", "model_runs", "kv", "combo_prediction_audit"}
        self.assertFalse(learned & set(RAW_RETENTION))
        self.assertEqual(RAW_RETENTION["flow_trades"], ("ts", 2))

    def test_trim_raw_reports_deleted_rows(self):
        self._flow("old", 3 * DAY)
        with self.store.lock:
            deleted = trim_raw(self.store.db, self.now)
            self.store.db.commit()
        self.assertEqual(deleted["flow_trades"], 1)

    def test_big_trim_compacts_right_away(self):
        from bot import maintenance
        old = maintenance.COMPACT_AFTER_ROWS
        maintenance.COMPACT_AFTER_ROWS = 3
        try:
            for i in range(5):
                self._flow(f"old{i}", 3 * DAY)
            result = maintenance.run_janitors(self.store, now=self.now, force=True)
            self.assertEqual(result["compacted_after_rows"], 5)
            self.assertEqual(self.store.get_kv("last_compact"), self.now)
            self._flow("old-again", 3 * DAY)          # a small trim does not compact
            result = maintenance.run_janitors(self.store, now=self.now + 1, force=True)
            self.assertNotIn("compacted_after_rows", result)
            self.assertEqual(self.store.get_kv("last_compact"), self.now)
        finally:
            maintenance.COMPACT_AFTER_ROWS = old

    def test_save_script_alerts_before_github_file_limit(self):
        script = (ROOT / ".github" / "save.sh").read_text(encoding="utf-8")
        self.assertIn("80000000", script)
        self.assertIn("95000000", script)
        # the size check runs before anything is pushed
        self.assertLess(script.index("95000000"), script.index("push_with_retry.sh"))


if __name__ == "__main__":
    unittest.main()


class TestLadderReadingThinning(unittest.TestCase):
    """Ladder strikes (~750 a run) are saved only when informative and at most
    every 30 minutes; 15-minute windows keep every reading."""

    def setUp(self):
        from bot.config import Settings
        from bot.engine import Engine
        from bot.signals.base import Reading
        self.R = Reading
        self.d = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.d.name, "t.db"))
        self.eng = Engine(Settings(), self.store, data_client=object(), llm=False)

    def tearDown(self):
        self.store.db.close()
        self.d.cleanup()

    def keep(self, series, ticker, mid, now):
        return self.eng._keep_crypto_readings({"series": series, "ticker": ticker},
                                              [self.R("kalshi_market", mid, 0.9)], now)

    def test_rules(self):
        now = 1_800_000_000
        self.assertTrue(self.keep("KXBTC15M", "KXBTC15M-W-00", 0.02, now))     # 15M: always
        self.assertFalse(self.keep("KXBTCD", "KXBTCD-X-T1", 0.03, now))        # far strike
        self.assertFalse(self.keep("KXBTCD", "KXBTCD-X-T1", 0.97, now))
        self.assertTrue(self.keep("KXBTCD", "KXBTCD-X-T2", 0.40, now))         # informative
        self.store.add_signal(now, "KXBTCD-X-T2", "kalshi_market", 0.4, 0.9, {})
        self.assertFalse(self.keep("KXBTCD", "KXBTCD-X-T2", 0.40, now + 600))  # too soon
        self.assertTrue(self.keep("KXBTCD", "KXBTCD-X-T2", 0.40, now + 1800))
