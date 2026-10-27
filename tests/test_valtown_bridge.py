import unittest
from unittest.mock import Mock, patch

from bot.valtown_bridge import bridge_get, eligible, _reset_for_tests


class ValTownBridgeTests(unittest.TestCase):
    def setUp(self):
        _reset_for_tests()

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

    def test_successful_metadata_is_cached_across_calls(self):
        response = Mock(status_code=200)
        response.json.return_value = {"markets": [{"ticker": "KXTEST"}]}
        env = {
            "VAL_TOWN_KALSHI_URL": "https://example.val.town",
            "ATI_INGEST_SHARED_TOKEN": "test-secret",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "bot.valtown_bridge.requests.get", return_value=response
        ) as request:
            first = bridge_get("/markets", {"status": "open"})
            second = bridge_get("/markets", {"status": "open"})
        self.assertEqual(first, second)
        request.assert_called_once()

    def test_cache_keys_include_query_parameters(self):
        response = Mock(status_code=200)
        response.json.return_value = {"events": []}
        env = {
            "VAL_TOWN_KALSHI_URL": "https://example.val.town",
            "ATI_INGEST_SHARED_TOKEN": "test-secret",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "bot.valtown_bridge.requests.get", return_value=response
        ) as request:
            bridge_get("/events", {"status": "open"})
            bridge_get("/events", {"status": "closed"})
        self.assertEqual(request.call_count, 2)

    def test_repeated_failures_open_cooldown_without_hiding_direct_fallback(self):
        response = Mock(status_code=503)
        env = {
            "VAL_TOWN_KALSHI_URL": "https://example.val.town",
            "ATI_INGEST_SHARED_TOKEN": "test-secret",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "bot.valtown_bridge.requests.get", return_value=response
        ) as request:
            for _ in range(3):
                self.assertEqual(bridge_get("/events"), (False, None))
            self.assertEqual(bridge_get("/events"), (False, None))
        self.assertEqual(request.call_count, 3)

    def test_bridge_never_caches_live_price_routes(self):
        self.assertEqual(bridge_get("/markets/KXTEST/orderbook"), (False, None))


if __name__ == "__main__":
    unittest.main()
