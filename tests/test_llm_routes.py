"""Each AI job tries its own free provider first so no single quota carries
everything: council -> Gemini, Don Theo and briefings -> GitHub Models.
The other providers stay as fallbacks."""
import os
import tempfile
import unittest
from unittest import mock

from bot import llm
from bot.storage import Store

ENV = {"GITHUB_MODELS_TOKEN": "ghs_x", "GEMINI_API_KEY": "g", "OPENROUTER_MODELS": "m1:free",
       "OMNIROUTE_URL": "", "LITELLM_URL": "", "GITHUB_MODELS_MODELS": "", "GEMINI_MODELS": "",
       "LLM_ROUTE_COUNCIL": "", "LLM_ROUTE_BRIEFING": "", "LLM_ROUTE_DON_THEO": ""}


class Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.tmp.name, "t.sqlite"))
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        def post(url, **kw):
            self.calls.append(url)
            return Resp()
        return llm.LLM("or-key", self.st, post=post, max_per_run=9, daily_limit=20)

    def test_each_task_goes_to_its_provider_first(self):
        with mock.patch.dict(os.environ, ENV):
            c = self.client()
            self.assertEqual(c.models_for("council")[0], "gemini|gemini-flash-latest")
            self.assertEqual(c.models_for("briefing")[0], "github|openai/gpt-4.1-mini")
            self.assertEqual(c.models_for("don_theo")[0], "github|openai/gpt-4.1-mini")
            self.assertEqual(c.models_for("other"), c.models)              # unrouted: unchanged
            self.assertEqual(sorted(c.models_for("council")), sorted(c.models))  # nothing dropped

    def test_override_and_missing_provider(self):
        with mock.patch.dict(os.environ, dict(ENV, LLM_ROUTE_COUNCIL="openrouter", GEMINI_API_KEY="")):
            c = self.client()
            self.assertEqual(c.models_for("council")[0], "m1:free")
        with mock.patch.dict(os.environ, dict(ENV, GEMINI_API_KEY="")):
            c = self.client()
            self.assertEqual(c.models_for("council"), c.models)            # no Gemini: normal order

    def test_preferred_model_skips_dead_and_tolerates_doubles(self):
        with mock.patch.dict(os.environ, ENV):
            c = self.client()
            c.dead.add("gemini|gemini-flash-latest")
            self.assertEqual(llm.preferred_model(c, "council"), "gemini|gemini-2.5-flash")
        self.assertIsNone(llm.preferred_model(object(), "council"))

    def test_briefing_and_don_theo_call_github_first(self):
        with mock.patch.dict(os.environ, ENV):
            c = self.client()
            text, used = c.ask("hi", model=llm.preferred_model(c, "don_theo"))
        self.assertEqual(used, "github|openai/gpt-4.1-mini")
        self.assertEqual(self.calls[0], llm.GITHUB_MODELS_URL)


class TestCouncilUsesRoute(unittest.TestCase):
    def test_council_orders_models_by_its_route(self):
        import inspect
        from bot import council
        src = inspect.getsource(council)
        self.assertIn('llm.models_for("council")', src)


if __name__ == "__main__":
    unittest.main()
