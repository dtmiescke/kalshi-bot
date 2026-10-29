"""Hourly ladder vs the 15-minute window that ends with it: same index, same instant, so the pairing pays at least $1."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from bot import cross_market
from bot.storage import Store

CLOSE = int(datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc).timestamp())          # 17:00 Eastern


def mk(strike, bid, ask):
    return {"floor_strike": strike, "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"}


class TestPieces(unittest.TestCase):
    def test_event_ticker_is_the_eastern_hour_and_only_on_the_hour(self):
        self.assertEqual(cross_market.event_ticker("KXBTC15M", CLOSE), "KXBTCD-26OCT2917")
        self.assertEqual(cross_market.event_ticker("KXETH15M", CLOSE), "KXETHD-26OCT2917")
        self.assertIsNone(cross_market.event_ticker("KXBTC15M", CLOSE + 900))           # a quarter past: no ladder ends then
        self.assertIsNone(cross_market.event_ticker("KXSOL15M", CLOSE))                  # no hourly SOL ladder

    def test_rungs_drop_unquoted_sides_and_sort(self):
        rungs = cross_market.rungs_from_markets([mk(80100, .30, .32), mk(80000, .45, .47), mk(80200, 0.0, .05),
                                                 {"floor_strike": 80300}, mk(80400, .02, .00)])
        self.assertEqual([r[0] for r in rungs], [80000, 80100])

    def test_a_consistent_market_has_no_riskless_pairing(self):
        rungs = [(80000, .60, .62), (80100, .40, .42)]
        up, dn = cross_market.best_trades(80050, .50, .52, rungs)
        self.assertLess(up[3], 0)
        self.assertLess(dn[3], 0)

    def test_a_cheap_15_minute_yes_against_a_dear_rung_above_is_riskless(self):
        rungs = [(80000, .60, .62), (80100, .55, .56)]                    # rung above the strike bids 55
        up, _ = cross_market.best_trades(80050, .30, .32, rungs)          # 15-minute YES offered at 32: 32 + (100-55) = 77 < 100
        self.assertGreater(up[3], 0.15)
        self.assertEqual(up[0], 80100)

    def test_a_cheap_rung_below_against_a_dear_15_minute_no_is_riskless(self):
        rungs = [(80000, .40, .42), (80100, .20, .22)]
        _, dn = cross_market.best_trades(80050, .60, .62, rungs)          # rung at/below: YES at 42, 15-minute NO at 40: 82 < 100
        self.assertGreater(dn[3], 0.10)
        self.assertEqual(dn[0], 80000)


class TestStudy(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.cm = cross_market.CrossMarket(self.store)

    def test_due_only_in_the_window_and_rate_limited(self):
        t = "KXBTC15M-26OCT291700-00"
        self.assertTrue(self.cm.due(t, CLOSE - 600, CLOSE))
        self.assertFalse(self.cm.due(t, CLOSE - 60, CLOSE))                  # too close to the end: spreads blow out
        self.assertFalse(self.cm.due(t, CLOSE - 1200, CLOSE))                # window not open yet
        self.cm.check(t, "KXBTC15M", CLOSE - 600, CLOSE, 80050, .50, .52, lambda ev: [mk(80000, .6, .62), mk(80100, .4, .42)])
        self.assertFalse(self.cm.due(t, CLOSE - 590, CLOSE))                 # within 30 s of the last check
        self.assertTrue(self.cm.due(t, CLOSE - 560, CLOSE))

    def test_records_a_check_and_reports_it(self):
        seen = []
        out = self.cm.check("KXBTC15M-26OCT291700-00", "KXBTC15M", CLOSE - 600, CLOSE, 80050, .30, .32,
                            lambda ev: seen.append(ev) or [mk(80000, .60, .62), mk(80100, .55, .56)])
        self.assertEqual(seen, ["KXBTCD-26OCT2917"])
        self.assertIsNotNone(out)
        rep = cross_market.report(self.store, now=CLOSE)
        self.assertEqual(rep["checks"], 1)
        self.assertEqual(rep["riskless"], 1)
        self.assertGreater(rep["best"], 15)

    def test_missing_data_or_a_failing_fetch_never_raises(self):
        def boom(ev):
            raise RuntimeError("network")
        self.assertIsNone(self.cm.check("t", "KXBTC15M", CLOSE - 600, CLOSE, 80050, .5, .52, boom))
        self.assertIsNone(self.cm.check("t2", "KXBTC15M", CLOSE - 600, CLOSE, None, .5, .52, lambda ev: []))
        self.assertIsNone(self.cm.check("t3", "KXBTC15M", CLOSE - 600, CLOSE + 900, 80050, .5, .52, lambda ev: []))   # not on the hour
        self.assertEqual(cross_market.report(self.store, now=CLOSE)["checks"], 0)

    def test_empty_report_is_safe(self):
        rep = cross_market.report(self.store, now=CLOSE)
        self.assertEqual(rep["checks"], 0)
        self.assertIsNone(rep["best"])


if __name__ == "__main__":
    unittest.main()
