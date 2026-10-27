"""Free AI fallbacks: GitHub Models (workflow token) and Gemini (free key) come
after OpenRouter's free models, only when configured, and a failing endpoint
falls through to the next."""
import os
import tempfile
import unittest
from unittest import mock

from bot import llm
from bot.config import Settings
from bot.storage import Store


class Resp:
    def __init__(self, ok, text="hi"):
        self.ok, self.text = ok, text

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("429")

    def json(self):
        return {"choices": [{"message": {"content": self.text}}]}


class TestFallbacks(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def test_unconfigured_adds_nothing(self):
        with mock.patch.dict(os.environ, {"GITHUB_MODELS_TOKEN": "", "GEMINI_API_KEY": ""}):
            names = [n for n, _, _ in llm.endpoints_from_env("or-key")]
            self.assertEqual(names, ["openrouter"])

    def test_order_and_fallthrough(self):
        env = {"GITHUB_MODELS_TOKEN": "ghs_x", "GEMINI_API_KEY": "g", "OPENROUTER_MODELS": "m1:free",
               "OMNIROUTE_URL": "", "LITELLM_URL": ""}
        calls = []

        def post(url, **kw):
            calls.append((url, kw["json"]["model"], kw["headers"]["Authorization"]))
            return Resp(ok="models.github.ai" in url)          # OpenRouter fails, GitHub answers
        with mock.patch.dict(os.environ, env):
            c = llm.LLM("or-key", self.st, post=post, max_per_run=5, daily_limit=10)
            self.assertEqual(c.models, ["m1:free", "github|openai/gpt-4.1-mini",
                                        "gemini|gemini-flash-latest", "gemini|gemini-2.5-flash"])
            text, used = c.ask("hello")
        self.assertEqual((text, used), ("hi", "github|openai/gpt-4.1-mini"))
        self.assertEqual(calls[0][0], llm.URL)
        self.assertEqual(calls[1], (llm.GITHUB_MODELS_URL, "openai/gpt-4.1-mini", "Bearer ghs_x"))

    def test_github_only_counts_as_ai_enabled(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "GITHUB_MODELS_TOKEN": "ghs_x"}):
            self.assertTrue(Settings().ai_enabled())
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "GITHUB_MODELS_TOKEN": "",
                                          "GEMINI_API_KEY": "", "OMNIROUTE_URL": "", "LITELLM_URL": ""}):
            self.assertFalse(Settings().ai_enabled())


if __name__ == "__main__":
    unittest.main()
