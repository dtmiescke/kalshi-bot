"""Regression tests for exactly-once early-exit ledger updates."""
import os
import tempfile
import unittest

from bot.storage import Store


class TestCloseBetIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.db"))
        self.bet_id = self.store.add_bet({
            "ts": 100, "mode": "paper", "ticker": "T", "event_ticker": "E",
            "series": "KXNBAGAME", "team": "X", "side": "yes", "price": 0.5,
            "contracts": 2, "stake": 1.0, "fee": 0.02, "model_prob": 0.6,
            "edge": 0.05, "confidence": 0.8, "signals": {},
        })

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_second_close_cannot_overwrite_realized_result(self):
        self.assertTrue(self.store.close_bet(self.bet_id, 0.7, 0.36))
        first = self.store.q("SELECT * FROM bets WHERE id=?", (self.bet_id,))[0]
        self.assertFalse(self.store.close_bet(self.bet_id, 0.1, -0.8))
        second = self.store.q("SELECT * FROM bets WHERE id=?", (self.bet_id,))[0]
        self.assertEqual(second["pnl"], first["pnl"])
        self.assertEqual(second["exit_price"], first["exit_price"])
        self.assertEqual(second["graded_ts"], first["graded_ts"])
        self.assertEqual(second["result"], "sold")
        self.assertEqual(len(self.store.open_bets()), 0)

    def test_close_of_missing_bet_returns_false(self):
        self.assertFalse(self.store.close_bet(999999, 0.7, 0.2))


if __name__ == "__main__":
    unittest.main()
