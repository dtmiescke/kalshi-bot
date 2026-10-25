"""Known problem 11: the phone app reads a small summary.json, not the whole DB."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bot import dashboard
from bot.config import Settings
from bot.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class TestAppSummary(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def test_summary_has_what_the_app_reads(self):
        now = int(time.time())
        self.st.set_kv("council_KXBTC15M-W-00", {"ts": now, "result": {
            "by": "council", "decision": "approve", "reason": "edge holds", "roles": {"x": 1}}})
        self.st.set_kv("council_KXBTC15M-W-01", {"ts": now, "result": {"by": "rules"}})  # not council
        self.st.add_decision_audit({"ts": now, "ticker": "T", "decision": "rejected", "signals": {}})
        self.st.x("UPDATE decision_audit SET outcome=1, brier=0.25")
        self.st.set_kv("janitors_last_result", {"ok": True})
        s = dashboard.summary(self.st)
        self.assertEqual([c["ticker"] for c in s["council"]], ["KXBTC15M-W-00"])
        self.assertEqual(s["council"][0]["ts"], now * 1000)
        self.assertEqual(s["audit"][0]["decision"], "rejected")
        self.assertEqual(s["kv"]["janitors_last_result"], {"ok": True})
        self.assertNotIn("nerd_last_health", s["kv"])            # absent keys left out

    def test_write_saves_summary_next_to_the_page(self):
        out = dashboard.write(self.st, Settings(), os.path.join(self.d.name, "site"))
        with open(os.path.join(out, "summary.json")) as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)

    def test_app_reads_summary_first_and_keeps_db_fallback(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('x.name==="summary.json"', html)
        self.assertLess(html.index('x.name==="summary.json"'), html.index('x.name==="bot.sqlite.gz"'))
        self.assertIn('x.name==="bot.sqlite.gz"', html)           # fallback kept


if __name__ == "__main__":
    unittest.main()
