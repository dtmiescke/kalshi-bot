"""Regression tests for duplicate order callbacks and idempotent settlement."""
import os
import tempfile
import unittest
from unittest import mock

from bot.storage import Store
from bot.grader import settle_bets


def bet(order_id="exchange-order-1"):
    return {
        "ts": 100, "mode": "demo", "ticker": "TICKER", "event_ticker": "EVENT",
        "series": "KXNBAGAME", "team": "Team A", "side": "yes", "price": 0.4,
        "contracts": 3, "stake": 1.2, "fee": 0.03, "model_prob": 0.7,
        "edge": 0.1, "confidence": 0.8, "signals": {}, "order_id": order_id,
    }


class TestOrderAndSettlementIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_repeated_order_callback_returns_same_ledger_row(self):
        first = self.store.add_bet(bet())
        second = self.store.add_bet({**bet(), "ts": 200, "contracts": 8, "stake": 3.2})
        rows = self.store.q("SELECT * FROM bets WHERE mode=? AND ticker=? AND order_id=?",
                            ("demo", "TICKER", "exchange-order-1"))
        self.assertEqual(first, second)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contracts"], 3)
        self.assertEqual(rows[0]["stake"], 1.2)

    def test_distinct_orders_remain_distinct(self):
        first = self.store.add_bet(bet("order-a"))
        second = self.store.add_bet(bet("order-b"))
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.q("SELECT id FROM bets")), 2)

    def test_settlement_updates_only_open_row_once(self):
        bet_id = self.store.add_bet(bet())
        client = mock.Mock()
        client.market.return_value = {"result": "yes"}
        with mock.patch("bot.grader.closing_yes", return_value=0.5):
            self.assertEqual(settle_bets(self.store, client), 1)
            first = dict(self.store.q("SELECT * FROM bets WHERE id=?", (bet_id,))[0])
            self.assertEqual(settle_bets(self.store, client), 0)
            second = dict(self.store.q("SELECT * FROM bets WHERE id=?", (bet_id,))[0])
        self.assertEqual(first["pnl"], second["pnl"])
        self.assertEqual(first["graded_ts"], second["graded_ts"])


if __name__ == "__main__":
    unittest.main()
