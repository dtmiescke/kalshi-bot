"""Deterministic paper-ledger integration checks; no network or real orders."""
import os
import tempfile
import unittest

from bot.storage import Store


class TestPaperLedgerPipeline(unittest.TestCase):
    def test_candidate_audit_and_paper_bet_are_distinct_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "paper.sqlite"))
            store.add_decision_audit({
                "ts": 1_800_000_000,
                "ticker": "TEST-MARKET",
                "event_ticker": "TEST-EVENT",
                "series": "TEST",
                "decision": "candidate",
                "reason": "deterministic fixture",
                "side": "yes",
                "market_price": 0.40,
                "model_prob": 0.55,
                "side_prob": 0.55,
                "edge": 15.0,
                "confidence": 0.8,
                "signals": {"fixture": True},
            })
            audit = store.q(
                "SELECT ticker, decision, side, edge FROM decision_audit WHERE ticker=?",
                ("TEST-MARKET",),
            )
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["decision"], "candidate")
            self.assertEqual(audit[0]["side"], "yes")
            self.assertAlmostEqual(audit[0]["edge"], 15.0)

            bet_id = store.add_bet({
                "ts": 1_800_000_000,
                "mode": "paper",
                "ticker": "TEST-MARKET",
                "event_ticker": "TEST-EVENT",
                "series": "TEST",
                "team": "Fixture",
                "side": "yes",
                "price": 0.40,
                "contracts": 2,
                "stake": 0.80,
                "fee": 0.02,
                "model_prob": 0.55,
                "edge": 15.0,
                "confidence": 0.8,
                "signals": {"fixture": True},
                "order_id": None,
                "style": "paper-test",
            })
            row = store.q(
                "SELECT id, mode, ticker, contracts, order_id, status FROM bets WHERE id=?",
                (bet_id,),
            )[0]
            self.assertEqual(row["mode"], "paper")
            self.assertEqual(row["ticker"], "TEST-MARKET")
            self.assertEqual(row["contracts"], 2)
            self.assertIsNone(row["order_id"])
            self.assertEqual(row["status"], "open")
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            store.db.close()


if __name__ == "__main__":
    unittest.main()
