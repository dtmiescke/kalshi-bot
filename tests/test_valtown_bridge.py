import unittest
from unittest.mock import Mock, patch

from bot.valtown_bridge import bridge_get, eligible

class ValTownBridgeTests(unittest.TestCase):
    def test_allowlist_only_public_metadata(self):
        self.assertTrue(eligible("/markets"))
        self.assertTrue(eligible("/events"))
        self.assertTrue(eligible("/markets/KXTEST-26"))
        self.assertFalse(eligible("/markets/KXTEST/orderbook"))
        self.assertFalse(eligible("/markets/orderbooks"))
        self.assertFalse(eligible("/markets/trades"))
        self.assertFalse(eligible("/portfolio/balance"))
        self.assertFalse(eligible("/portfolio/events/orders", "POST"))

    def test_missing_config_uses_direct_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(bridge_get("/markets"), (False, None))

    def test_http_error_uses_direct_fallback(self):
        response = Mock(status_code=503)
        with patch.dict("os.environ", {
            "VAL_TOWN_KALSHI_URL": "https://example.val.town",
            "ATI_INGEST_SHARED_TOKEN": "test-secret",
        }, clear=True), patch("bot.valtown_bridge.requests.get", return_value=response):
            self.assertEqual(bridge_get("/markets"), (False, None))

    def test_valid_json_dict_is_returned(self):
        response = Mock(status_code=200)
        response.json.return_value = {"markets": [{"ticker": "KXTEST"}]}
        with patch.dict("os.environ", {
            "VAL_TOWN_KALSHI_URL": "https://example.val.town",
            "ATI_INGEST_SHARED_TOKEN": "test-secret",
        }, clear=True), patch("bot.valtown_bridge.requests.get", return_value=response):
            handled, payload = bridge_get("/markets", {"status": "open"})
        self.assertTrue(handled)
        self.assertEqual(payload["markets"][0]["ticker"], "KXTEST")

if __name__ == "__main__":
    unittest.main()