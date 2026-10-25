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


if __name__ == "__main__":
    unittest.main()
