"""Signal report card: each crypto signal scored against Kalshi's own price on
the same settled markets, plus the bot's blended forecast."""
import json
import os
import tempfile
import time
import unittest

from bot import dashboard
from bot.storage import Store


class TestSignalReport(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        now = int(time.time())
        for i in range(10):
            ticker, y = f"KXBTC15M-26OCT28{i:04d}-00", i % 2
            # Kalshi said 50% every time; "good" leaned the right way, "bad" the wrong way
            for sig, p in (("kalshi_market", 0.5), ("crypto_model", 0.7 if y else 0.3),
                           ("crypto_momentum", 0.3 if y else 0.7)):
                self.st.x("INSERT INTO signal_grades VALUES(?,?,?,?,?,?,?,?)",
                          (now - 60, None, sig, p, y, (p - y) ** 2, None, ticker))
            self.st.x("""INSERT INTO decision_audit(ts,ticker,series,decision,model_prob,signals_json,outcome,brier,settled_ts)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (now - 300, ticker, "KXBTC15M", "rejected", 0.6 if y else 0.4,
                       json.dumps({"kalshi_market": {"p": 0.5, "c": 0.9}}), y, 0.16, now))
        # sports rows are ignored
        self.st.x("INSERT INTO signal_grades VALUES(?,?,?,?,?,?,?,?)",
                  (now - 60, None, "crypto_model", 0.9, 0, 0.81, None, "KXNFLGAME-X"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def test_scores_each_signal_against_the_market_on_the_same_markets(self):
        rows = {r["signal"]: r for r in dashboard.signal_report(self.st)}
        self.assertEqual(rows["crypto_model"]["n"], 10)
        self.assertEqual(rows["crypto_model"]["market_brier"], 0.25)
        self.assertGreater(rows["crypto_model"]["skill"], 0)       # beat Kalshi
        self.assertLess(rows["crypto_momentum"]["skill"], 0)       # Kalshi was better
        self.assertAlmostEqual(rows["_blend"]["skill"], 1 - 0.16 / 0.25, places=3)
        self.assertNotIn("kalshi_market", rows)
        self.assertEqual({r["type"] for r in dashboard.signal_report(self.st)}, {"15m"})
        self.assertEqual(dashboard.signal_report(self.st)[0]["signal"], "_blend")   # listed first

    def test_summary_carries_it_for_the_app(self):
        self.assertTrue(any(r["signal"] == "crypto_model" for r in dashboard.summary(self.st)["signals"]))

    def test_decided_markets_are_left_out(self):
        # Kalshi already at 97c: a "right" signal there is no tradable edge
        now = int(time.time())
        for i in range(10):
            t = f"KXETH15M-26OCT28{i:04d}-00"
            for sig, p in (("kalshi_market", 0.97), ("crypto_orderbook", 0.999)):
                self.st.x("INSERT INTO signal_grades VALUES(?,?,?,?,?,?,?,?)", (now, None, sig, p, 1, (p - 1) ** 2, None, t))
        self.assertNotIn("crypto_orderbook", {r["signal"] for r in dashboard.signal_report(self.st)})

    def test_brain_learns_only_from_markets_that_were_still_a_bet(self):
        from bot import brain
        decided = [{"kalshi_market": 0.97, "crypto_model": 0.999, "_outcome": 1}] * 40
        self.assertEqual(brain.learn(self.st, decided, "crypto"), {})          # nothing to learn from
        live = [{"kalshi_market": 0.5, "crypto_model": 0.3, "_outcome": 1}] * 40
        self.assertLess(brain.learn(self.st, live, "crypto")["crypto_model"][1], 1.2)   # lost to Kalshi: weight drops

    def test_needs_enough_markets(self):
        self.assertEqual(dashboard.signal_report(self.st, min_n=50), [])


if __name__ == "__main__":
    unittest.main()
