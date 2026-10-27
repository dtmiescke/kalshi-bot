"""Regression tests for the live crypto runner's four-coin wiring."""
import unittest

from bot.live.runner import COINS, COINS_TO_COIN


class FourCoinRunnerConfigurationTests(unittest.TestCase):
    def test_exactly_requested_four_15m_series_are_wired(self):
        expected = {"KXBTC15M", "KXBNB15M", "KXSOL15M", "KXETH15M"}
        self.assertEqual(set(COINS), expected)
        self.assertEqual(set(COINS_TO_COIN), expected)

    def test_series_and_learning_coin_names_match(self):
        self.assertEqual(
            {series: COINS_TO_COIN[series] for series in COINS},
            {
                "KXBTC15M": "BTC",
                "KXETH15M": "ETH",
                "KXSOL15M": "SOL",
                "KXBNB15M": "BNB",
            },
        )

    def test_no_missing_coinbase_product_is_subscribed(self):
        # BNB deliberately uses its Kraken pair only unless a supported
        # Coinbase product is verified; None must never be a product ID.
        self.assertIsNone(COINS["KXBNB15M"][1])
        products = [coinbase for _, coinbase, _ in COINS.values() if coinbase]
        self.assertNotIn(None, products)
        self.assertEqual(len(products), len(set(products)))

    def test_each_coin_has_a_kraken_pair(self):
        self.assertTrue(all(kraken for kraken, _, _ in COINS.values()))
        self.assertEqual(len({kraken for kraken, _, _ in COINS.values()}), 4)


if __name__ == "__main__":
    unittest.main()
