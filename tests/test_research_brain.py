import os
import unittest
from unittest.mock import Mock, patch

from bot import research_brain


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get_kv(self, key, default=None):
        return self.values.get(key, default)

    def set_kv(self, key, value):
        self.values[key] = value


class FakeLLM:
    endpoints = {"openrouter": ("https://openrouter.ai/api/v1/chat/completions", "key")}

    def __init__(self):
        self.calls = []

    def search_web(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return ("Found current evidence with https://news.example.org/item", "test-model",
                ["https://news.example.org/item", "https://official.example.com/status"])


class TestResearchBrain(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.llm = FakeLLM()

    def test_event_research_is_cached_by_event_and_keeps_provenance(self):
        with patch.dict(os.environ, {"SPORTS_AI_WEB_SEARCH": "yes"}):
            first = research_brain.event_research(
                self.llm, self.store, event_ticker="KX-NBA-123", sport="basketball_nba",
                team="Home", opponent="Away", event_start_ts=2000, now=1000000)
            second = research_brain.event_research(
                self.llm, self.store, event_ticker="KX-NBA-123", sport="basketball_nba",
                team="Home", opponent="Away", event_start_ts=2000, now=1100)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(first["description"], second["description"])
        self.assertEqual(first["event_ticker"], "KX-NBA-123")
        self.assertEqual(first["research_sources"][0]["domain"], "news.example.org")
        self.assertTrue(self.store.get_kv("sports_web_research_health")["ok"])

    def test_disabled_event_search_makes_no_calls(self):
        with patch.dict(os.environ, {"SPORTS_AI_WEB_SEARCH": "no"}):
            self.assertIsNone(research_brain.event_research(
                self.llm, self.store, event_ticker="E1", sport="nba",
                team="Home", opponent="Away", now=1000))
        self.assertEqual(self.llm.calls, [])

    def test_tech_scout_runs_once_per_week_and_never_auto_applies(self):
        with patch.dict(os.environ, {"OPENROUTER_TECH_SCOUT_ENABLED": "yes"}):
            result = research_brain.run_tech_scout(self.store, self.llm, now=1000)
            again = research_brain.run_tech_scout(self.store, self.llm, now=1001000)
        self.assertTrue(result["ok"])
        self.assertFalse(self.store.get_kv("openrouter_tech_scout_latest")["automatic_changes"])
        self.assertEqual(again["skipped"], "weekly cadence")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertTrue(self.llm.calls[0][1]["allow_fetch"])


if __name__ == "__main__":
    unittest.main()
