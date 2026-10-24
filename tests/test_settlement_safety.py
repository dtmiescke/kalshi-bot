"""Regression tests for deterministic settlement deadline parsing."""
import os
import time
import unittest

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

    def test_malformed_strike_and_settlement_window_do_not_crash_discovery(self):
        original = os.environ.get("SETTLE_AVG_SECONDS")
        try:
            os.environ["SETTLE_AVG_SECONDS"] = "not-a-number"
            terms = settlement.contract_terms({"floor_strike": "bad", "close_time": "2026-10-19T18:30:00Z"})
            self.assertIsNone(terms["strike"])
            self.assertEqual(terms["avg_seconds"], 60)
        finally:
            if original is None:
                os.environ.pop("SETTLE_AVG_SECONDS", None)
            else:
                os.environ["SETTLE_AVG_SECONDS"] = original


    def test_probability_rejects_nonfinite_or_invalid_inputs(self):
        for args in (
            (float("nan"), 100, 0.01, 30),
            (100, float("inf"), 0.01, 30),
            (100, 100, float("nan"), 30),
            (100, 100, 0.01, float("inf")),
            (100, 100, 0.01, 30, 0),
            (100, 100, 0.01, 30, 60, -1),
        ):
            with self.subTest(args=args):
                self.assertIsNone(settlement.prob_above(*args))
        self.assertIsNone(settlement.prob_above(100, 100, 0.01, 30, realized_avg=float("nan")))

    def test_invalid_proxy_uncertainty_config_uses_safe_default(self):
        original = os.environ.get("SETTLE_PROXY_BPS")
        try:
            os.environ["SETTLE_PROXY_BPS"] = "nan"
            result = settlement.prob_above(100, 100, 0.01, 30)
            self.assertIsNotNone(result)
            self.assertGreaterEqual(result, 0.0)
            self.assertLessEqual(result, 1.0)
        finally:
            if original is None:
                os.environ.pop("SETTLE_PROXY_BPS", None)
            else:
                os.environ["SETTLE_PROXY_BPS"] = original


if __name__ == "__main__":
    unittest.main()
