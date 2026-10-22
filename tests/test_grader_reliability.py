"""Regression tests for bounded grading fairness and model-monitor isolation."""
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import grader
from bot.storage import Store


class SettledClient:
    def market(self, ticker):
        return {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": "KXNBAGAME",
            "result": "yes",
        }


class TestGradingReliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_learning_cursor_prevents_unsettled_early_rows_starving_later_markets(self):
        # The first 150 tickers are missing start metadata. They used to occupy
        # the same LIMIT 150 forever, preventing later settled markets learning.
        for i in range(200):
            ticker = f"A{i:03d}"
            for signal in ("ml_model", "kalshi_market"):
                self.store.x(
                    "INSERT INTO signal_readings(ts,ticker,signal,prob,confidence,detail) "
                    "VALUES(?,?,?,?,?,?)",
                    (1, ticker, signal, 0.6, 0.8, "{}"),
                )
            if i >= 150:
                self.store.set_kv(f"start_{ticker}", 1)
                self.store.set_kv(f"settle_after_{ticker}", 2)

        with patch("bot.grader.ml.label"), patch("bot.grader.brain.learn", return_value={}):
            first_n, _ = grader.learn_from_markets(self.store, SettledClient(), max_fetch=150)
            self.assertEqual(first_n, 0)
            self.assertEqual(self.store.get_kv("learning_cursor_ticker"), "A149")

            second_n, _ = grader.learn_from_markets(self.store, SettledClient(), max_fetch=150)

        self.assertEqual(second_n, 50)
        self.assertEqual(
            self.store.q("SELECT COUNT(*) AS n FROM graded_markets")[0]["n"], 50
        )
        self.assertEqual(
            self.store.q("SELECT COUNT(*) AS n FROM signal_grades")[0]["n"], 100
        )

    def test_model_decay_monitor_pairs_same_crypto_contract_and_ignores_sports(self):
        # Give the crypto model poor forecasts and the market proxy good ones.
        # Sports rows are deliberately the reverse; mixing domains would mask decay.
        for i in range(100):
            ticker = f"KXBTC15M-26OCT22{i:03d}-00"
            self.store.x(
                "INSERT INTO signal_grades(ts,bet_id,signal,prob,outcome,brier,clv_direction,segment) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (1000 + i, None, "ml_model", 0.8, 0, 0.30, None, ticker),
            )
            self.store.x(
                "INSERT INTO signal_grades(ts,bet_id,signal,prob,outcome,brier,clv_direction,segment) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (1000 + i, None, "kalshi_market", 0.5, 0, 0.10, None, ticker),
            )
        for i in range(100):
            ticker = f"KXNFLGAME-26OCT22{i:03d}"
            self.store.x(
                "INSERT INTO signal_grades(ts,bet_id,signal,prob,outcome,brier,clv_direction,segment) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (5000 + i, None, "ml_model", 0.5, 0, 0.0, None, ticker),
            )
            self.store.x(
                "INSERT INTO signal_grades(ts,bet_id,signal,prob,outcome,brier,clv_direction,segment) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (5000 + i, None, "kalshi_market", 0.9, 0, 0.5, None, ticker),
            )

        with patch("bot.ml.MODEL_DIR", self.tmp.name):
            result = grader.monitor_model(self.store, window=100)

        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["n"], 100)
        self.assertAlmostEqual(result["ml_brier"], 0.30)
        self.assertAlmostEqual(result["kalshi_brier"], 0.10)


if __name__ == "__main__":
    unittest.main()
