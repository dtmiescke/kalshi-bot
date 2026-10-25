"""Regression tests for consolidated read-only MCP and freshness safeguards."""
import unittest
from unittest.mock import patch

from bot.market_rules import summarize_rules
from bot.marketdata import _cached


class MemoryStore:
    def __init__(self):
        self.data = {}

    def get_kv(self, key):
        return self.data.get(key)

    def set_kv(self, key, value):
        self.data[key] = value


class ConsolidatedResearchSafetyTests(unittest.TestCase):
    def test_conflicting_nested_settlement_source_fails_closed(self):
        result = summarize_rules({
            "rules_primary": "Official league result",
            "settlement_source": "League A",
            "result": {"settlement_source": "League B"},
        })
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["available"])

    def test_identical_nested_rules_are_allowed(self):
        result = summarize_rules({
            "rules_primary": "Official league result",
            "result": {
                "rules_primary": "Official league result",
                "settlement_source": "League A",
            },
        })
        self.assertEqual(result["status"], "terms_found")

    def test_fresh_cache_reused(self):
        store = MemoryStore()
        store.data["x"] = {"ts": 995, "v": {"price": 1}}
        with patch("bot.marketdata.time.time", return_value=1000):
            self.assertEqual(_cached(store, "x", 1, lambda: self.fail("unexpected fetch")), {"price": 1})

    def test_stale_cache_does_not_become_live_evidence(self):
        store = MemoryStore()
        store.data["x"] = {"ts": 1, "v": {"price": 1}}
        with patch("bot.marketdata.time.time", return_value=1000):
            self.assertIsNone(_cached(store, "x", 1, lambda: None))

    def test_corrupt_or_future_timestamp_fails_closed(self):
        store = MemoryStore()
        with patch("bot.marketdata.time.time", return_value=1000):
            for ts in ("invalid", 2000, None):
                store.data["x"] = {"ts": ts, "v": {"price": 1}}
                self.assertIsNone(_cached(store, "x", 1, lambda: None))

    def test_expired_cache_replaced_on_recovery(self):
        store = MemoryStore()
        store.data["x"] = {"ts": 1, "v": 1}
        with patch("bot.marketdata.time.time", return_value=1000):
            self.assertEqual(_cached(store, "x", 1, lambda: 2), 2)
        self.assertEqual(store.data["x"]["v"], 2)


if __name__ == "__main__":
    unittest.main()
