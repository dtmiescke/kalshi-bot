import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from bot.llm import LLM, URL


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get_kv(self, key, default=None):
        return self.values.get(key, default)

    def set_kv(self, key, value):
        self.values[key] = value


class TestOpenRouterWebSearch(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.response = Mock()
        self.response.raise_for_status.return_value = None
        self.response.json.return_value = {
            "choices": [{"message": {
                "content": "Official update: https://example.org/story",
                "citations": [{"url": "https://example.org/story"}],
            }}]
        }
        self.post = Mock(return_value=self.response)
        self.llm = LLM(
            "test-key", self.store, models=["openai/gpt-5.2"],
            max_per_run=4, daily_limit=10, post=self.post,
            endpoints={"openrouter": (URL, "test-key")},
        )

    def test_web_search_uses_openrouter_server_tool_and_returns_sources(self):
        text, model, sources = self.llm.search_web("Research this game", max_results=4)
        self.assertIn("Official update", text)
        self.assertEqual(model, "openai/gpt-5.2")
        self.assertEqual(sources, ["https://example.org/story"])
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["tools"][0]["type"], "openrouter:web_search")
        self.assertEqual(payload["tools"][0]["parameters"]["max_results"], 4)
        self.assertLessEqual(payload["max_tool_calls"], 5)

    def test_tech_scout_fetch_is_domain_restricted(self):
        self.llm.search_web("Study public repo designs", allow_fetch=True)
        payload = self.post.call_args.kwargs["json"]
        fetch_tool = payload["tools"][1]
        self.assertEqual(fetch_tool["type"], "openrouter:web_fetch")
        self.assertIn("github.com", fetch_tool["parameters"]["allowed_domains"])
        self.assertNotIn("example.com", fetch_tool["parameters"]["allowed_domains"])

    def test_missing_openrouter_key_does_not_attempt_search(self):
        llm = LLM(None, self.store, models=["openai/gpt-5.2"], post=self.post, endpoints={})
        self.assertEqual(llm.search_web("search"), (None, None, []))
        self.post.assert_not_called()

    def test_daily_search_budget_fails_closed(self):
        with patch.dict(os.environ, {"OPENROUTER_WEB_SEARCH_DAILY_LIMIT": "1"}):
            self.store.set_kv("openrouter_web_searches_" + __import__("time").strftime("%Y%m%d", __import__("time").gmtime()), 1)
            self.assertEqual(self.llm.search_web("search"), (None, None, []))
            self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
