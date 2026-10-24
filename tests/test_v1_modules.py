import random
import unittest

from bot import fees, health_checks as hc, kalshi_history as kh, don_theo as dt


class TestFees(unittest.TestCase):
    def test_fee_peaks_near_50c(self):
        self.assertGreater(fees.fee(0.50, 100), fees.fee(0.10, 100))
        self.assertGreater(fees.fee(0.50, 100), fees.fee(0.90, 100))

    def test_fee_rounds_up_to_cent(self):
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> 0.02
        self.assertAlmostEqual(fees.fee(0.50, 1), 0.02)

    def test_maker_cheaper_than_taker(self):
        self.assertLess(fees.fee(0.5, 100, maker=True), fees.fee(0.5, 100))

    def test_edge_after_fees_can_erase_edge(self):
        # 2c gross edge at 50c is wiped out by the 2c fee on one contract
        self.assertLessEqual(fees.edge_after_fees(0.52, 0.50), 0.0)

    def test_settle_pnl(self):
        f = fees.fee(0.11, 20)
        self.assertAlmostEqual(fees.settle_pnl(0.11, 20, True), round(20 - 2.2 - f, 2))
        self.assertAlmostEqual(fees.settle_pnl(0.11, 20, False), round(-2.2 - f, 2))

    def test_bad_price(self):
        with self.assertRaises(ValueError):
            fees.fee(1.2, 1)


class TestHealthChecks(unittest.TestCase):
    def test_orderbook_bug_is_caught(self):
        stats = {"pricing": {"ladders": {"seen": 488, "priced": 0}}}
        alarms = hc.run_all(stats)
        self.assertTrue(any(a.severity == "critical" and a.check == "pricing" for a in alarms))
        self.assertEqual(hc.paused_areas(alarms), ["ladders"])

    def test_healthy_run_is_quiet(self):
        stats = {"pricing": {"ladders": {"seen": 120, "priced": 30}},
                 "sports": {"NBA": {"games_scheduled": 8, "markets_found": 40}}}
        self.assertEqual(hc.run_all(stats), [])

    def test_sports_league_missing(self):
        stats = {"sports": {"NFL": {"games_scheduled": 1, "markets_found": 0}}}
        self.assertEqual(hc.paused_areas(hc.run_all(stats)), ["sports"])

    def test_ungraded_bets(self):
        bets = [{"ticker": "OLD", "expected_settle_ts": 1000}, {"ticker": "NEW", "expected_settle_ts": 9000}]
        alarms = hc.check_ungraded_bets(bets, now=9100)
        self.assertEqual(alarms[0].data["tickers"], ["OLD"])

    def test_missing_fields_counted(self):
        recs = [{"price": None, "size": 3}, {"price": 0.4, "size": None}, {"price": None, "size": 1}]
        a = hc.check_fields(recs, ["price", "size"], "orderbook")
        self.assertEqual(a[0].data["missing"], {"price": 2, "size": 1})
        self.assertEqual(a[0].severity, "critical")

    def test_broken_check_becomes_alarm(self):
        alarms = hc.run_all({"pricing": "not a dict"})
        self.assertTrue(any("check failed" in a.message for a in alarms))


class FakeClient:
    def __init__(self, live=None, hist=None, live_trades=None, hist_trades=None):
        self.live, self.hist = live or {}, hist or {}
        self.lt, self.ht = live_trades or [], hist_trades or []

    def _req(self, method, path, params=None):
        if path.startswith("/historical/markets/"):
            t = path.rsplit("/", 1)[1]
            return {"market": self.hist.get(t)} if t in self.hist else {}
        if path.startswith("/markets/"):
            t = path.rsplit("/", 1)[1]
            return {"market": self.live.get(t)} if t in self.live else {}
        if path == "/historical/cutoff":
            return {"market_settled_ts": 123}
        raise RuntimeError("unexpected path " + path)

    def _paged(self, path, key, params, max_pages=5):
        return list(self.ht if path.startswith("/historical") else self.lt)


class TestHistory(unittest.TestCase):
    def test_live_first(self):
        r = kh.HistoryRouter(FakeClient(live={"A": {"result": "yes"}}))
        self.assertEqual(r.market("A")[1], "live")
        self.assertEqual(r.fallbacks["market"], 0)

    def test_archive_fallback(self):
        r = kh.HistoryRouter(FakeClient(hist={"OLD": {"result": "no", "settlement_value_dollars": "0"}}))
        result, value, src = r.settlement("OLD")
        self.assertEqual((result, src), ("no", "archive"))
        self.assertEqual(r.fallbacks["market"], 1)

    def test_missing_everywhere(self):
        self.assertEqual(kh.HistoryRouter(FakeClient()).market("X"), (None, "missing"))

    def test_trades_merged_and_deduped(self):
        c = FakeClient(live_trades=[{"trade_id": 1}, {"trade_id": 2}],
                       hist_trades=[{"trade_id": 2}, {"trade_id": 3}])
        rows = kh.HistoryRouter(c).trades("T")
        self.assertEqual(sorted(t["trade_id"] for t in rows), [1, 2, 3])


class TestDonTheo(unittest.TestCase):
    rng = random.Random(1)

    def test_reason_always_attached(self):
        out = dt.on_decision(False, "edge 1.2% after fees", rng=self.rng)
        self.assertEqual(out["reason"], "edge 1.2% after fees")

    def test_streak_line(self):
        self.assertIn("I AM the streak", dt.on_settle(5, 2, 8, "r", rng=self.rng)["line"])

    def test_roast_doubter(self):
        self.assertIn("Professor", dt.on_settle(5, 2, 1, "r", doubters=["situationist"], rng=self.rng)["line"])

    def test_losses_humble_him(self):
        self.assertIn(dt.on_settle(-3, 3, -4, "r", rng=self.rng)["line"], dt.LINES["loss_streak"])

    def test_moods(self):
        self.assertEqual(dt.mood(50, 6), "hot_hand")
        self.assertEqual(dt.mood(-10, 1), "grumpy")
        self.assertEqual(dt.mood(10, 1), "smooth")
        self.assertEqual(dt.mood(0, 0), "all_business")

    def test_bad_news_first(self):
        self.assertTrue(dt.bad_news("Odds feed is down.")["line"].startswith("Odds feed is down."))


if __name__ == "__main__":
    unittest.main()
