"""Regression tests for deterministic settlement deadline parsing."""
import os
import time
import unittest
from unittest.mock import patch

from bot.live import settlement


class TestSettlementTimestampSafety(unittest.TestCase):
    def test_naive_close_time_is_interpreted_as_utc_not_host_local_time(self):
        close = "2026-10-19T18:30:00"
        original_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Los_Angeles"
            if hasattr(time, "tzset"):
                time.tzset()
            terms = settlement.contract_terms({"floor_strike": 100000, "close_time": close})
            expected = 1792434600  # 2026-10-19 18:30:00 UTC
            self.assertEqual(terms["close_ts"], expected)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            if hasattr(time, "tzset"):
                time.tzset()

    def test_explicit_utc_and_offset_deadlines_keep_their_instant(self):
        utc = settlement.contract_terms({"close_time": "2026-10-19T18:30:00Z"})
        offset = settlement.contract_terms({"close_time": "2026-10-19T20:30:00+02:00"})
        self.assertEqual(utc["close_ts"], offset["close_ts"])

    def test_malformed_deadline_fails_closed(self):
        terms = settlement.contract_terms({"floor_strike": 100000, "close_time": "not-a-time"})
        self.assertIsNone(terms["close_ts"])


if __name__ == "__main__":
    unittest.main()
