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

    def test_split_rejects_zero_negative_full_and_oversized_sales(self):
        for sold in (0, -1, 2, 3):
            with self.subTest(sold=sold):
                with self.assertRaises(ValueError):
                    self.store.split_bet(self.bet_id, sold)
        row = self.store.q("SELECT contracts, status FROM bets WHERE id=?", (self.bet_id,))[0]
        self.assertEqual((row["contracts"], row["status"]), (2, "open"))

    def test_split_is_atomic_and_preserves_total_contracts_and_cost(self):
        sold_id = self.store.split_bet(self.bet_id, 1)
        rows = self.store.q("SELECT * FROM bets WHERE id IN (?, ?) ORDER BY id",
                            (self.bet_id, sold_id))
        self.assertEqual(sum(r["contracts"] for r in rows), 2)
        self.assertAlmostEqual(sum(r["stake"] for r in rows), 1.0)
        self.assertAlmostEqual(sum(r["fee"] for r in rows), 0.02)
        self.assertTrue(all(r["status"] == "open" for r in rows))

    def test_split_rejects_already_closed_bet(self):
        self.assertTrue(self.store.close_bet(self.bet_id, 0.7, 0.36))
        with self.assertRaises(ValueError):
            self.store.split_bet(self.bet_id, 1)


if __name__ == "__main__":
    unittest.main()
