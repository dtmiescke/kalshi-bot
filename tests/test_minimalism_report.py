"""Regression tests for concise LLM prompts and readable performance reports."""
import os
import tempfile
import unittest
from unittest.mock import patch

from bot.llm import LLM
from bot.storage import Store
from bot.report import build


class FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class TestMinimalismAndPlainEnglish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_minimalism_adds_system_policy_by_default(self):
        captured = {}

        def post(url, json=None, **kwargs):
            captured.update(json)
            return FakeResponse()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_MINIMALISM_ENABLED", None)
            llm = LLM("key", self.store, models=["test-model"], post=post)
            self.assertEqual(llm.ask("Return OK."), ("ok", "test-model"))
        self.assertEqual(captured["messages"][0]["role"], "system")
        self.assertIn("Never change probabilities", captured["messages"][0]["content"])
        self.assertEqual(captured["messages"][-1], {"role": "user", "content": "Return OK."})

    def test_minimalism_can_be_disabled_for_compatibility(self):
        captured = {}

        def post(url, json=None, **kwargs):
            captured.update(json)
            return FakeResponse()

        with patch.dict(os.environ, {"LLM_MINIMALISM_ENABLED": "no"}):
            llm = LLM("key", self.store, models=["test-model"], post=post)
            self.assertEqual(llm.ask("Return OK."), ("ok", "test-model"))
        self.assertEqual(captured["messages"], [{"role": "user", "content": "Return OK."}])

    def test_report_explains_brier_and_common_terms(self):
        report = build(self.store)
        self.assertIn("prediction error (Brier)", report)
        self.assertIn("P&L = dollars won/lost after fees", report)
        self.assertIn("CLV (closing-line value)", report)
        self.assertIn("Calibration =", report)



    def test_schema_driven_json_avoids_extra_policy_tokens(self):
        captured = {}

        class JsonResponse(FakeResponse):
            def json(self):
                return {"choices": [{"message": {"content": '{"decision":"wait"}'}}]}

        def post(url, json=None, **kwargs):
            captured.update(json)
            return JsonResponse()

        llm = LLM("key", self.store, models=["test-model"], post=post)
        self.assertEqual(llm.ask_json("Return JSON with decision.")[0], {"decision": "wait"})
        self.assertEqual(captured["messages"], [{"role": "user", "content": "Return JSON with decision."}])

if __name__ == "__main__":
    unittest.main()
