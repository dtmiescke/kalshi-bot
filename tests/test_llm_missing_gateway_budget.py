"""Regression tests for gateway routing without spending unused call budget."""
import unittest
from unittest.mock import Mock

from bot.llm import LLM


class Store:
    def __init__(self):
        self.data = {}

    def get_kv(self, key, default=None):
        return self.data.get(key, default)

    def set_kv(self, key, value):
        self.data[key] = value


class RoutingBudgetTests(unittest.TestCase):
    def test_missing_gateway_does_not_consume_budget(self):
        store = Store()
        post = Mock()
        bot = LLM("", store, models=["litellm|missing-model"],
                  endpoints=[("openrouter", "https://example.test/chat/completions", "key")],
                  post=post, max_per_run=3, daily_limit=3)
        self.assertEqual(bot.ask("hello"), (None, None))
        self.assertEqual(bot.calls_this_run, 0)
        self.assertEqual(bot.budget_left(), 3)
        post.assert_not_called()

    def test_missing_gateway_falls_through_to_configured_gateway(self):
        store = Store()
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        post = Mock(return_value=response)
        bot = LLM("", store, models=["litellm|missing-model", "openrouter|valid"],
                  endpoints=[("openrouter", "https://example.test/chat/completions", "key")],
                  post=post, max_per_run=3, daily_limit=3)
        self.assertEqual(bot.ask("hello"), ("ok", "openrouter|valid"))
        self.assertEqual(bot.calls_this_run, 1)
        post.assert_called_once()

    def test_ordinary_calls_preserve_reserved_web_research_budget(self):
        store = Store()
        post = Mock()
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        post.return_value = response
        bot = LLM("key", store, models=["openrouter|valid"], endpoints=[
            ("openrouter", "https://example.test/chat/completions", "key")
        ], post=post, max_per_run=4, daily_limit=10)
        with __import__("unittest").mock.patch.dict("os.environ", {
            "LLM_WEB_SEARCH_RESERVE": "2",
            "SPORTS_AI_WEB_SEARCH": "yes",
            "OPENROUTER_WEB_SEARCH_MAX_PER_RUN": "2",
            "OPENROUTER_WEB_SEARCH_DAILY_LIMIT": "6",
        }):
            self.assertEqual(bot.budget_left(), 2)
            self.assertEqual(bot.budget_left(for_web_search=True), 4)
            bot.ask("routine council call")
            bot.ask("routine council call")
            self.assertEqual(bot.budget_left(), 0)
            self.assertEqual(bot.budget_left(for_web_search=True), 2)


if __name__ == "__main__":
    unittest.main()
