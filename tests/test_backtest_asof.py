"""The backtest's one-pass walk-forward lookups (_AsOf) must give exactly what
the old per-market queries gave: _weights_as_of and brain.calibration_table."""
import os
import random
import tempfile
import unittest

from bot import backtest, brain
from bot.storage import Store

SERIES = ("KXNBAGAME", "KXBTC15M", "KXBTCD", "KXNFLGAME")


class TestAsOf(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        rnd = random.Random(11)
        for i in range(300):
            ts = rnd.randint(0, 10_000)
            sig = rnd.choice(("book_consensus", "crypto_model", "trade_flow"))
            name = sig + rnd.choice(("", "@sports", "@crypto"))
            self.st.x("INSERT INTO weight_history VALUES(?,?,?,?)",
                      (ts * 1_000_000_000, name, rnd.uniform(0.1, 3), "x"))
        for i in range(400):
            self.st.add_decision_audit({"ts": i, "ticker": f"T{i}", "series": rnd.choice(SERIES),
                                        "decision": "candidate", "model_prob": rnd.random()})
            settled = rnd.choice((None, rnd.randint(0, 10_000)))
            self.st.x("UPDATE decision_audit SET outcome=?, brier=?, settled_ts=? WHERE ticker=?",
                      (rnd.randint(0, 1), 0.2, settled, f"T{i}"))
        # bets only for a domain with no audit rows early on (fallback path)
        for i in range(50):
            self.st.x("""INSERT INTO bets (ts, mode, ticker, series, model_prob, status, result,
                         graded_ts) VALUES(?,?,?,?,?,?,?,?)""",
                      (i, "paper", f"B{i}", "KXNBAGAME", rnd.random(), "graded",
                       rnd.choice(("yes", "no")), rnd.randint(0, 3000)))
        self.st.x("DELETE FROM decision_audit WHERE series='KXNBAGAME' AND settled_ts < 3000")

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def test_matches_per_case_queries(self):
        asof = backtest._AsOf(self.st)
        rnd = random.Random(5)
        for dom in ("sports", "crypto"):
            for cutoff in sorted(rnd.randint(-10, 11_000) for _ in range(120)):
                self.assertEqual(asof.weights_for(dom, cutoff),
                                 backtest._weights_as_of(self.st, dom, cutoff), (dom, cutoff))
                new = asof.calibration_table(dom, cutoff)
                old = brain.calibration_table(self.st, domain=dom, as_of_ts=cutoff)
                self.assertEqual(new.keys(), old.keys(), (dom, cutoff))
                for b in old:
                    self.assertEqual(new[b]["n"], old[b]["n"])
                    self.assertAlmostEqual(new[b]["actual"], old[b]["actual"])
                for p in (0.05, 0.35, 0.62, 0.97):
                    self.assertAlmostEqual(brain.calibrate_with_table(p, new),
                                           brain.calibrate(p, self.st, domain=dom, as_of_ts=cutoff))


if __name__ == "__main__":
    unittest.main()
