"""Hourly trading-target safety tests."""
import os
import tempfile
import time
import unittest

from bot import risk
from bot.config import Settings
from bot.storage import Store


class TestHourlyCryptoTradeCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.db"))
        self.settings = Settings()
        self.settings.mode = "paper"
        self.settings.crypto_max_trades_per_hour = 10

    def tearDown(self):
        self.tmp.cleanup()

    def test_crypto_entries_stop_at_ten_per_rolling_hour(self):
        now = int(time.time())
        for i in range(10):
            self.store.add_bet({
                "ts": now - i * 60, "mode": "paper",
                "ticker": f"KXBTC15M-W{i}", "event_ticker": f"KXBTC15M-E{i}",
                "series": "KXBTC15M", "team": "UP", "side": "yes",
                "price": 0.50, "contracts": 30, "stake": 15.0, "fee": 0.50,
                "model_prob": 0.65, "edge": 0.10, "confidence": 0.9,
                "signals": [], "status": "graded",
            })
        plan, reason = risk.decide(
            self.settings, self.store,
            {"ticker": "KXBTC15M-NEXT", "event_ticker": "KXBTC15M-NEXT",
             "series": "KXBTC15M"},
            0.80, 0.95, {},
        )
        self.assertIsNone(plan)
        self.assertIn("hourly trade cap reached (10/hour)", reason)

    def test_non_crypto_trade_not_limited_by_crypto_hourly_cap(self):
        self.settings.crypto_max_trades_per_hour = 10
        # No price book is supplied; reaching the ordinary pricing check proves
        # this sports candidate was not rejected by the crypto-only hourly cap.
        plan, reason = risk.decide(
            self.settings, self.store,
            {"ticker": "KXNFLGAME-NEXT", "event_ticker": "KXNFLGAME-NEXT",
             "series": "KXNFLGAME"},
            0.80, 0.95, {},
        )
        self.assertNotIn("hourly trade cap", reason)


if __name__ == "__main__":
    unittest.main()
