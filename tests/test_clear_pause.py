"""The execution hard stop must not be cleared without explicit reconciliation."""
import os
import tempfile
import unittest

from bot.main import clear_pause
from bot.storage import Store


class TestClearPauseConfirmation(unittest.TestCase):
    def test_refuses_to_clear_without_confirmation_and_preserves_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "test.sqlite"))
            store.set_kv("execution_reconciliation_required", "order status unknown")
            with self.assertRaisesRegex(RuntimeError, "CONFIRM_RECONCILED=yes"):
                clear_pause(store, confirmed=False)
            self.assertEqual(
                store.get_kv("execution_reconciliation_required"), "order status unknown"
            )

    def test_clears_only_after_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "test.sqlite"))
            store.set_kv("execution_reconciliation_required", "order status unknown")
            reason = clear_pause(store, confirmed=True)
            self.assertEqual(reason, "order status unknown")
            self.assertIsNone(store.get_kv("execution_reconciliation_required"))


if __name__ == "__main__":
    unittest.main()
