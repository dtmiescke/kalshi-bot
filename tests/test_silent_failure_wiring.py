"""T2c: health_checks.run_all runs on each cycle's real output, merged into the Nerd."""
import os
import tempfile
import time
import unittest

from bot import guard, nerd
from bot.storage import Store


class TestSilentFailureCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.now = int(time.time())
        self.alerts = []

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def snapshots(self, n, priced, prefix="KXNBAGAME"):
        for i in range(n):
            self.st.add_market_snapshot({
                "ts": self.now, "ticker": f"{prefix}-E{i}-T", "event_ticker": f"{prefix}-E{i}",
                "series": prefix, "title": "", "team": "T",
                "yes_bid": 0.4 if i < priced else None, "yes_ask": 0.45 if i < priced else None,
                "last_price": None, "volume": 0, "open_interest": 0, "close_time": "", "status": "open"})

    def check(self):
        return nerd.silent_failure_check(self.st, self.now - 60, now=self.now,
                                         alert=lambda *a, **k: self.alerts.append(a))

    def test_unpriced_sports_pauses_sports_only_until_clean(self):
        self.snapshots(30, 0)                       # the 0-of-488 class of bug
        result = self.check()
        self.assertEqual(result["paused_areas"], ["sports"])
        self.assertTrue(guard.league_benched(self.st, "KXNBAGAME"))
        self.assertFalse(guard.league_benched(self.st, "KXBTC15M"))     # crypto keeps trading
        self.assertEqual(len(self.alerts), 1)
        self.check()                                # still broken: no repeat alert
        self.assertEqual(len(self.alerts), 1)
        # next cycle prices markets again: pause lifts by itself
        self.now += 900
        self.snapshots(30, 30)
        self.assertEqual(self.check()["paused_areas"], [])
        self.assertFalse(guard.league_benched(self.st, "KXNBAGAME"))

    def test_healthy_cycle_has_no_alarms_and_is_in_nerd_report(self):
        self.snapshots(30, 28)
        self.st.set_kv("nerd_last_health", {"ok": True, "checks": {"quick_check": ["ok"]}})
        result = self.check()
        self.assertEqual(result["alarms"], [])
        report = self.st.get_kv("nerd_last_health")
        self.assertTrue(report["ok"])                                   # Nerd's own result kept
        self.assertEqual(report["silent_failures"]["stats"]["pricing"]["sports"],
                         {"seen": 30, "priced": 28})

    def test_few_markets_never_pause(self):
        self.snapshots(3, 0, prefix="KXBTC15M")     # 3 crypto windows: too few to judge
        self.assertEqual(self.check()["paused_areas"], [])

    def test_rejections_without_outside_signals_do_not_pause_everything(self):
        # e.g. odds quota exhausted: sports rejected for "no outside signal"
        self.st.add_decision_audit({"ts": self.now, "ticker": "KXNBAGAME-E-T", "decision": "rejected",
                                    "reason": "no outside signal", "signals": {}})
        self.assertEqual(self.check()["paused_areas"], [])

    def test_candidates_with_no_stored_signals_pause_all(self):
        self.st.add_decision_audit({"ts": self.now, "ticker": "KXBTC15M-W-00", "decision": "candidate",
                                    "reason": "passed deterministic risk checks", "signals": {}})
        result = self.check()
        self.assertEqual(result["paused_areas"], ["all"])
        self.assertTrue(guard.league_benched(self.st, "KXBTC15M"))

    def test_stale_open_bet_is_a_warning_not_a_pause(self):
        self.st.add_bet({"ts": self.now - 7200, "mode": "paper", "ticker": "KXBTC15M-W-00",
                         "event_ticker": "KXBTC15M-W", "series": "KXBTC15M", "team": "UP",
                         "side": "yes", "price": .4, "contracts": 1, "stake": .4, "fee": .01,
                         "model_prob": .5, "edge": .05, "confidence": .8, "signals": {}})
        self.st.set_kv("settle_after_KXBTC15M-W-00", self.now - 3600)
        result = self.check()
        self.assertEqual([a["check"] for a in result["alarms"]], ["ungraded_bets"])
        self.assertEqual(result["paused_areas"], [])

    def test_area_names(self):
        self.assertEqual(nerd.health_area("KXETH15M-W-00"), "crypto")
        self.assertEqual(nerd.health_area("KXBTCD-26OCT2517"), "ladders")
        self.assertEqual(nerd.health_area("KXNFLGAME-X"), "sports")


if __name__ == "__main__":
    unittest.main()
