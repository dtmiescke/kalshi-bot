"""Contract tests for additive, read-only Kalshi official data endpoints."""
import unittest
from unittest.mock import Mock

from bot.kalshi_client import KalshiClient


class OfficialDataEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.client = KalshiClient("https://external-api.kalshi.com/trade-api/v2")
        self.client._req = Mock(return_value={"live_data": {"details": {"status": "live"}}})

    def test_paths_are_read_only(self):
        cases = [
            ("sports_filters", (), "/search/filters_by_sport"),
            ("milestone_live_data", ("game-123",), "/live_data/milestone/game-123"),
            ("milestone_game_stats", ("game-123",), "/live_data/milestone/game-123/game_stats"),
            ("event_live_data", ("KXBTC-26",), "/live_data/events/KXBTC-26"),
        ]
        for name, args, path in cases:
            with self.subTest(name=name):
                self.client._req.reset_mock()
                getattr(self.client, name)(*args)
                self.client._req.assert_called_once_with("GET", path)

    def test_rejects_path_injection_before_network(self):
        for value in ("", "../orders", "x?foo=bar", "a/b", "x#fragment", "a b", None):
            for method in (self.client.milestone_live_data,
                           self.client.milestone_game_stats,
                           self.client.event_live_data):
                with self.subTest(value=value, method=method.__name__):
                    with self.assertRaises(ValueError):
                        method(value)
        self.client._req.assert_not_called()

    def test_missing_data_is_not_fabricated(self):
        self.client._req.return_value = {"live_data": None}
        self.assertEqual(self.client.milestone_live_data("game-123"), {"live_data": None})


if __name__ == "__main__":
    unittest.main()
