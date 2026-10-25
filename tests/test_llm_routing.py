import os
import unittest
from unittest.mock import Mock, patch

from bot.llm import LLM, URL, endpoints_from_env


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get_kv(self, key, default=None):
        return self.values.get(key, default)

    def set_kv(self, key, value):
        self.values[key] = value


class TestLLMGatewayRouting(unittest.TestCase):
    def test_gateway_priority_is_omniroute_litellm_openrouter(self):
        with patch.dict(os.environ, {
            "OMNIROUTE_URL": "https://omni.example/v1",
            "OMNIROUTE_API_KEY": "omni-key",
            "LITELLM_URL": "https://litellm.example/v1",
            "LITELLM_API_KEY": "litellm-key",
        }, clear=True):
            endpoints = endpoints_from_env("openrouter-key")
        self.assertEqual([x[0] for x in endpoints], ["omniroute", "litellm", "openrouter"])
        self.assertEqual(endpoints[1][1], "https://litellm.example/v1/chat/completions")
        self.assertEqual(endpoints[1][2], "litellm-key")

    def test_litellm_full_chat_url_is_not_duplicated(self):
        with patch.dict(os.environ, {
            "OMNIROUTE_URL": "",
            "LITELLM_URL": "https://litellm.example/v1/chat/completions",
        }, clear=True):
            endpoints = endpoints_from_env(None)
        self.assertEqual(endpoints, [
            ("litellm", "https://litellm.example/v1/chat/completions", "")
        ])

    def test_litellm_model_alias_is_routed_through_proxy(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }
        post = Mock(return_value=response)
        store = MemoryStore()
        with patch.dict(os.environ, {
            "OMNIROUTE_URL": "",
            "LITELLM_URL": "https://litellm.example/v1",
            "LITELLM_API_KEY": "proxy-key",
            "LITELLM_MODELS": "sports-fast,reasoning-strong",
            "OPENROUTER_MODELS": "openai/gpt-4o-mini",
        }, clear=True):
            llm = LLM(None, store, post=post)
            text, used = llm.ask("hello")
        self.assertEqual(text, "ok")
        self.assertEqual(used, "litellm|sports-fast")
        self.assertEqual(post.call_args.args[0], "https://litellm.example/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "sports-fast")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer proxy-key")

    def test_failed_omniroute_falls_through_to_litellm(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": "fallback worked"}}]}

        def fake_post(url, **kwargs):
            if url.startswith("https://omni.example/"):
                raise __import__("requests").ConnectionError("gateway unavailable")
            return response

        store = MemoryStore()
        with patch.dict(os.environ, {
            "OMNIROUTE_URL": "https://omni.example/v1",
            "OMNIROUTE_API_KEY": "omni-key",
            "OMNIROUTE_MODELS": "auto",
            "LITELLM_URL": "https://litellm.example/v1",
            "LITELLM_API_KEY": "litellm-key",
            "LITELLM_MODELS": "fallback-model",
            "OPENROUTER_MODELS": "openai/gpt-4o-mini",
        }, clear=True):
            llm = LLM(None, store, post=fake_post)
            text, used = llm.ask("hello")
        self.assertEqual(text, "fallback worked")
        self.assertEqual(used, "litellm|fallback-model")
        self.assertIn("omniroute", llm.dead)

    def test_blank_litellm_settings_preserve_openrouter_fallback(self):
        with patch.dict(os.environ, {
            "OMNIROUTE_URL": "",
            "LITELLM_URL": "",
            "LITELLM_MODELS": "ignored-alias",
            "OPENROUTER_MODELS": "openai/gpt-4o-mini",
        }, clear=True):
            endpoints = endpoints_from_env("openrouter-key")
            llm = LLM("openrouter-key", MemoryStore(), endpoints=endpoints)
        self.assertEqual([x[0] for x in endpoints], ["openrouter"])
        self.assertEqual(llm.models, ["openai/gpt-4o-mini"])


if __name__ == "__main__":
    unittest.main()
