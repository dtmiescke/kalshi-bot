"""Startup execution recovery must fail closed after uncertain orders."""
import unittest

from bot.live.runner import LiveRunner


class TestStartupReconciliationGate(unittest.TestCase):
    def test_persisted_reconciliation_marker_blocks_demo_and_live(self):
        for mode in ("demo", "live"):
            with self.subTest(mode=mode):
                reason = LiveRunner._startup_reconciliation_reason(
                    mode, None, "previous order status unknown"
                )
                self.assertEqual(reason, "previous order status unknown")

    def test_unresolved_maker_intent_blocks_demo_and_live(self):
        for mode in ("demo", "live"):
            with self.subTest(mode=mode):
                reason = LiveRunner._startup_reconciliation_reason(
                    mode, {"state": "submitting"}, None
                )
                self.assertIn("reconcile Kalshi", reason)

    def test_paper_mode_is_not_blocked_by_live_exchange_reconciliation(self):
        self.assertIsNone(LiveRunner._startup_reconciliation_reason(
            "paper", {"state": "submitting"}, "previous order status unknown"
        ))

    def test_clean_demo_or_live_start_has_no_reconciliation_block(self):
        for mode in ("demo", "live"):
            with self.subTest(mode=mode):
                self.assertIsNone(LiveRunner._startup_reconciliation_reason(mode, None, None))


if __name__ == "__main__":
    unittest.main()
