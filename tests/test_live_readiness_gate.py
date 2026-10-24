"""Live mode requires both explicit confirmation and separate operational approval."""
import os
import unittest
from unittest.mock import patch

from bot.config import Settings


class TestLiveReadinessGate(unittest.TestCase):
    def settings(self):
        return Settings(
            mode="live",
            live_confirmed=True,
            kalshi_key_id="test-key",
            kalshi_private_key="test-private-key",
        )

    def test_live_refused_without_operational_approval(self):
        with patch.dict(os.environ, {"LIVE_TRADING_OPERATIONAL_APPROVED": "no"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "operational readiness has not been approved"):
                self.settings().validate()

    def test_live_refused_when_approval_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIVE_TRADING_OPERATIONAL_APPROVED", None)
            with self.assertRaisesRegex(RuntimeError, "operational readiness has not been approved"):
                self.settings().validate()

    def test_live_can_pass_config_gate_only_after_both_confirmations(self):
        with patch.dict(os.environ, {"LIVE_TRADING_OPERATIONAL_APPROVED": "yes"}, clear=False):
            self.settings().validate()

    def test_paper_mode_does_not_require_live_approval(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIVE_TRADING_OPERATIONAL_APPROVED", None)
            Settings(mode="paper", live_confirmed=False).validate()


if __name__ == "__main__":
    unittest.main()
