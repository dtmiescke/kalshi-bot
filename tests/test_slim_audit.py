"""decision_audit and sports news were 570 MB of an 888 MB memory file.
Old audit snapshots keep each signal's probability/confidence and small tags
but drop bulky details; calibration columns are untouched. Sports news keeps 3 days."""
import json
import os
import tempfile
import unittest
import unittest.mock

from bot import maintenance, storage
from bot.storage import Store

NOW = 1_800_000_000


class TestSlimAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.tmp.name, "t.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def audit(self, age_days, signals):
        self.st.add_decision_audit({"ts": NOW - int(age_days * 86400), "ticker": "KXBTC15M-X", "series": "KXBTC15M",
                                    "decision": "rejected", "model_prob": 0.61, "market_price": 0.55,
                                    "signals": signals})

    def test_old_bulky_snapshots_slimmed_recent_kept(self):
        big = {"crypto_model": {"prob": 0.6123456, "confidence": 0.75, "detail": {"z": "x" * 3000}},
               "ml_model": {"p": 0.58, "c": 0.9},
               "regime": "calm", "price_discovery": {"sources": {"a": "y" * 2000}}}
        self.audit(10, big)
        self.audit(1, big)
        res = maintenance.janitor_slim_audit(self.st, now=NOW)
        self.assertEqual(res["slimmed"], 1)
        self.assertGreater(res["bytes_saved"], 4000)
        old, new = self.st.q("SELECT signals_json, model_prob FROM decision_audit ORDER BY ts")
        slim = json.loads(old["signals_json"])
        self.assertEqual(slim, {"crypto_model": {"p": 0.6123, "c": 0.75}, "ml_model": {"p": 0.58, "c": 0.9},
                                "regime": "calm"})
        self.assertEqual(old["model_prob"], 0.61)                    # calibration data untouched
        self.assertIn("price_discovery", json.loads(new["signals_json"]))   # recent rows keep details
        self.assertEqual(maintenance.janitor_slim_audit(self.st, now=NOW)["slimmed"], 0)  # idempotent

    def test_run_janitors_reports_and_compacts_after_big_slim(self):
        self.audit(10, {"crypto_model": {"prob": 0.6, "confidence": 0.7, "detail": {"z": "x" * 5000}}})
        with unittest.mock.patch.object(maintenance, "COMPACT_AFTER_BYTES", 1000), \
                unittest.mock.patch.object(self.st, "compact") as compact:
            res = maintenance.run_janitors(self.st, now=NOW, force=True)
        self.assertEqual(res["janitor_c"]["slimmed"], 1)
        compact.assert_called_once()

    def test_sports_news_keeps_three_days(self):
        self.assertEqual(storage.RAW_RETENTION["sports_intel_items"], ("observed_ts", 3))



if __name__ == "__main__":
    unittest.main()
