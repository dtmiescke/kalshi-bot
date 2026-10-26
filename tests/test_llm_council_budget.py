import os
import unittest
import time
from unittest.mock import patch

from bot.llm import LLM


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get_kv(self, key, default=None):
        return self.values.get(key, default)

    def set_kv(self, key, value):
        self.values[key] = value


class TestCouncilBudgetReserve(unittest.TestCase):
    def test_ordinary_calls_leave_daily_capacity_for_council(self):
        store = MemoryStore()
        with patch.dict(os.environ, {
            "LLM_COUNCIL_RESERVE": "4",
            "LLM_WEB_SEARCH_RESERVE": "0",
            "COUNCIL_MODE": "advisory",
        }, clear=False):
            llm = LLM(
                None, store, models=["omniroute|auto"], daily_limit=10,
                max_per_run=6,
                endpoints=[("omniroute", "https://omni.example/v1/chat/completions", "key")],
            )
            store.set_kv("llm_calls_" + time.strftime("%Y%m%d", time.gmtime()), 8)
            self.assertEqual(llm.budget_left(), 0)
            self.assertEqual(llm.budget_left(for_council=True), 2)

    def test_disabled_council_does_not_reserve_capacity(self):
        store = MemoryStore()
        with patch.dict(os.environ, {
            "LLM_COUNCIL_RESERVE": "4",
            "LLM_WEB_SEARCH_RESERVE": "0",
            "COUNCIL_MODE": "off",
        }, clear=False):
            llm = LLM(
                None, store, models=["omniroute|auto"], daily_limit=10,
                max_per_run=6,
                endpoints=[("omniroute", "https://omni.example/v1/chat/completions", "key")],
            )
            self.assertEqual(llm.budget_left(), 6)


if __name__ == "__main__":
    unittest.main()
