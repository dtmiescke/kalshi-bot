"""Regression checks for the official Kalshi demo API endpoint.

Keep demo trading separate from paper-market data and live execution.
"""
import unittest
from unittest.mock import patch

from bot.config import KALSHI_URLS, Settings


class DemoEndpointTests(unittest.TestCase):
    def test_recommended_demo_rest_endpoint(self):
        self.assertEqual(
            KALSHI_URLS["demo"],
            "https://external-api.demo.kalshi.co/trade-api/v2",
        )

    def test_demo_and_paper_use_demo_order_endpoint(self):
        with patch.dict("os.environ", {"KALSHI_BASE_URL": ""}):
            for mode in ("demo", "paper"):
                with self.subTest(mode=mode):
                    self.assertEqual(Settings(mode=mode).base_url(), KALSHI_URLS["demo"])

    def test_paper_research_keeps_real_market_data(self):
        with patch.dict("os.environ", {"KALSHI_DATA_URL": ""}):
            self.assertEqual(
                Settings(mode="paper").market_data_url(), KALSHI_URLS["production"]
            )

    def test_live_order_endpoint_remains_unchanged(self):
        with patch.dict("os.environ", {"KALSHI_BASE_URL": ""}):
            self.assertEqual(
                Settings(mode="live").base_url(), KALSHI_URLS["production"]
            )

    def test_explicit_demo_override_remains_supported(self):
        url = "https://custom-demo.example/trade-api/v2"
        with patch.dict("os.environ", {"KALSHI_BASE_URL": url}):
            self.assertEqual(Settings(mode="demo").base_url(), url)


if __name__ == "__main__":
    unittest.main()
