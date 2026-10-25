"""Regression checks: external research data must never be silently reused stale."""
import unittest
from unittest.mock import patch

from bot.marketdata import _cached


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get_kv(self, key):
        return self.values.get(key)

    def set_kv(self, key, value):
        self.values[key] = value


class MarketContextFreshnessTests(unittest.TestCase):
    def test_recent_cached_value_avoids_network(self):
        store = MemoryStore()
        store.values["sample"] = {"ts": 995.0, "v": {"price": 1}}
        with patch("bot.marketdata.time.time", return_value=1000.0):
            self.assertEqual(_cached(store, "sample", 1, lambda: self.fail("unexpected fetch")), {"price": 1})

    def test_stale_cache_not_returned_when_fetch_fails(self):
        store = MemoryStore()
        store.values["sample"] = {"ts": 1.0, "v": {"price": 1}}
        with patch("bot.marketdata.time.time", return_value=1000.0):
            self.assertIsNone(_cached(store, "sample", 1, lambda: None))

    def test_stale_cache_refreshed_when_source_recovers(self):
        store = MemoryStore()
        store.values["sample"] = {"ts": 1.0, "v": {"price": 1}}
        with patch("bot.marketdata.time.time", return_value=1000.0):
            self.assertEqual(_cached(store, "sample", 1, lambda: {"price": 2}), {"price": 2})
        self.assertEqual(store.values["sample"]["v"], {"price": 2})

    def test_future_dated_cache_is_not_trusted(self):
        store = MemoryStore()
        store.values["sample"] = {"ts": 2000.0, "v": {"price": 1}}
        with patch("bot.marketdata.time.time", return_value=1000.0):
            self.assertIsNone(_cached(store, "sample", 1, lambda: None))

    def test_corrupt_cache_does_not_crash(self):
        store = MemoryStore()
        store.values["sample"] = {"ts": "broken", "v": {"price": 1}}
        with patch("bot.marketdata.time.time", return_value=1000.0):
            self.assertIsNone(_cached(store, "sample", 1, lambda: None))


if __name__ == "__main__":
    unittest.main()
