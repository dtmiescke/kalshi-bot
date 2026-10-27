import unittest
from unittest.mock import patch

from bot.config import CRYPTO_15M
from bot.live import feeds
from bot.live.runner import COINS, COINS_TO_COIN, LiveRunner


class TestCryptoRunnerCoverage(unittest.TestCase):
    def test_four_configured_series_have_runtime_feed_and_coin_identity(self):
        self.assertEqual(set(CRYPTO_15M), set(COINS))
        self.assertEqual(set(COINS), set(COINS_TO_COIN))
        self.assertEqual(COINS["KXBNB15M"], ("BNB/USD", None, None))
        self.assertEqual(COINS_TO_COIN["KXBNB15M"], "BNB")

    def test_coinbase_subscriptions_skip_products_not_listed_there(self):
        by_product = {
            product: object()
            for _, (_, product, _) in COINS.items()
            if product
        }
        self.assertIn("BTC-USD", by_product)
        self.assertIn("ETH-USD", by_product)
        self.assertIn("SOL-USD", by_product)
        self.assertNotIn("BNB-USD", by_product)
        self.assertEqual(
            feeds.coinbase_subs(list(by_product)),
            [{"type": "subscribe", "product_ids": list(by_product),
              "channels": ["ticker", "matches"]}],
        )

    def test_bnb_history_seed_does_not_call_coinbase_or_abort(self):
        runner = LiveRunner.__new__(LiveRunner)
        runner.seed_candles = {}
        class FakeAnalytics:
            def __init__(self):
                self.mids = []
        runner.coins = {series: FakeAnalytics() for series in COINS}
        with patch("bot.live.runner.crypto_sig.fetch_candles",
                   side_effect=AssertionError("BNB must not call Coinbase")) as fetch:
            runner.seed_history()
        self.assertEqual(runner.seed_candles["KXBNB15M"], [])
        self.assertEqual(fetch.call_count, 3)

    def test_coinbase_history_failure_falls_back_to_live_feed(self):
        runner = LiveRunner.__new__(LiveRunner)
        runner.seed_candles = {}
        class FakeAnalytics:
            def __init__(self):
                self.mids = []
        runner.coins = {series: FakeAnalytics() for series in COINS}
        with patch("bot.live.runner.crypto_sig.fetch_candles",
                   side_effect=RuntimeError("provider unavailable")):
            runner.seed_history()
        self.assertEqual(set(runner.seed_candles), set(COINS))
        self.assertTrue(all(rows == [] for rows in runner.seed_candles.values()))


if __name__ == "__main__":
    unittest.main()
