"""Regression tests for the profit-leak audit (see FLAWS.md). Each test pins
one leak shut so a future change can't silently reopen it."""
import os
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timezone

from bot import brain, guard, history, ml, risk
from bot.config import Settings
from bot.live import settlement
from bot.live.analytics import SymbolAnalytics
from bot.live.book import OrderBook
from bot.storage import Store


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bet(store, **kw):
    b = {"ts": int(time.time()), "mode": "paper", "ticker": "T", "event_ticker": "E",
         "series": "KXNBAGAME", "team": "X", "side": "yes", "price": 0.5, "contracts": 10,
         "stake": 5, "fee": 0.2, "model_prob": 0.6, "edge": 0.05, "confidence": 0.8, "signals": {}}
    b.update(kw)
    return store.add_bet(b)


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()

    def tearDown(self):
        self.d.cleanup()

    def test_daily_loss_breaker_separates_paper_and_live_pnl(self):
        now = int(time.time())
        start = risk.day_start_ts(now, "America/Chicago")
        self.st.x("""INSERT INTO bets(ts,mode,ticker,event_ticker,series,side,price,contracts,
                     stake,fee,status,result,pnl,graded_ts)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (now, "paper", "PAPER-LOSS", "PAPER-E", "KXNBAGAME", "yes",
                   0.5, 1, 0.5, 0.01, "graded", "no", -100.0, now))
        with mock.patch.dict(os.environ, {"BOT_TIMEZONE": "America/Chicago"}):
            allowed, reason = guard.status(self.st, 1000, daily_loss_limit=30, mode="live")
        self.assertTrue(allowed, reason)
        self.st.x("""INSERT INTO bets(ts,mode,ticker,event_ticker,series,side,price,contracts,
                     stake,fee,status,result,pnl,graded_ts)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (now, "live", "LIVE-LOSS", "LIVE-E", "KXNBAGAME", "yes",
                   0.5, 1, 0.5, 0.01, "graded", "no", -35.0, now))
        allowed, reason = guard.status(self.st, 1000, daily_loss_limit=30, mode="live")
        self.assertFalse(allowed)
        self.assertIn("daily loss limit", reason)

    def test_league_loss_guard_separates_paper_and_live(self):
        now = int(time.time())
        self.st.x("""INSERT INTO bets(ts,mode,ticker,event_ticker,series,side,price,contracts,
                     stake,fee,status,result,pnl,graded_ts)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (now, "paper", "PAPER-CRYPTO", "PAPER-E", "KXBTC15M", "yes",
                   0.5, 1, 0.5, 0.01, "graded", "no", -10.0, now))
        self.assertFalse(guard.league_benched(self.st, "KXBTC15M", n=1, mode="live"))
        self.assertTrue(guard.league_benched(self.st, "KXBTC15M", n=1, mode="paper"))

    def test_daily_risk_day_start_matches_central_dashboard_day(self):
        # 04:30 UTC on Oct 21 is 23:30 CDT on Oct 20.
        now = datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc).timestamp()
        start = risk.day_start_ts(now, "America/Chicago")
        expected = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(start, int(expected))
        # In winter, Central midnight is 06:00 UTC because of standard time.
        winter_now = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc).timestamp()
        winter_start = risk.day_start_ts(winter_now, "America/Chicago")
        winter_expected = datetime(2026, 1, 14, 6, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(winter_start, int(winter_expected))

    def test_01_history_has_no_lookahead(self):
        """Spot jumps in the minute AFTER the Kalshi candle must not be visible."""
        close = 1_800_000_000
        m = {"ticker": "KXBTC15M-L", "floor_strike": 100000, "result": "yes", "close_time": iso(close)}
        t = close - 600                              # Kalshi candle ending at t
        spot = {t - 60 * k: 100000.0 + (k % 3) for k in range(1, 120)}
        spot[t] = 200000.0                           # candle that STARTS at t = the future
        rows = history.replay_market(m, [{"end_period_ts": t, "yes_bid": {"close": 50},
                                           "yes_ask": {"close": 52}}], spot, 0.07)
        self.assertTrue(rows)
        self.assertLess(rows[0]["model"], 0.9)      # it did NOT see the $200k print

    def test_02_proxy_error_tempers_near_strike_certainty(self):
        tight = settlement.prob_above(100001, 100000, 1e-6, 30, 60, proxy_bps=0)
        real = settlement.prob_above(100001, 100000, 1e-6, 30, 60, proxy_bps=3)
        self.assertGreater(tight, 0.99)
        self.assertLess(real, 0.7)                   # index gap makes it near a coin flip

    def test_03_crypto_judged_at_decision_time(self):
        self.assertEqual(brain.decision_cutoff("KXBTC15M-X-00", 1000), 880)
        self.assertEqual(brain.decision_cutoff("KXNBAGAME-X-BOS", 1000), 1000)

    def test_04_real_fee_edge_check(self):
        # $1 = 10 contracts at $0.10: per-100 fee estimate says 3.03% edge, but the
        # real order's fee rounds up from 6.3c to 7c and leaves only 2.96%
        # The 25% earned-size floor means a $4 request produces this $1 test budget.
        self.s.fixed_bet = 4.0
        book = {"yes": [(0.09, 50)], "no": [(0.90, 50)]}
        plan, why = risk.decide(self.s, self.st, {"ticker": "T", "event_ticker": "E"},
                                0.1366, 0.9, book, min_edge=0.03)
        self.assertIsNone(plan)
        self.assertIn("real fees", why)

    def test_04b_final_stake_includes_fee_within_budget(self):
        self.s.fixed_bet = 10.0
        self.s.max_per_bet = 1.0
        self.s.min_edge = 0.01
        book = {"yes": [(0.49, 100)], "no": [(0.50, 100)]}
        plan, why = risk.decide(self.s, self.st, {"ticker": "T", "event_ticker": "E",
                                  "series": "KXNFLGAME"}, 0.8, 0.9, book)
        self.assertIsNotNone(plan, why)
        self.assertLessEqual(plan["stake"] + plan["fee"], 1.0 + 1e-9)

    def test_04c_fixed_bet_respects_earned_size_ramp(self):
        self.s.fixed_bet = 20.0
        self.s.max_per_bet = 20.0
        self.s.min_edge = 0.01
        book = {"yes": [(0.49, 100)], "no": [(0.49, 100)]}
        plan, why = risk.decide(self.s, self.st, {"ticker": "T", "event_ticker": "E",
                                  "series": "KXNFLGAME"}, 0.8, 0.9, book)
        self.assertIsNotNone(plan, why)
        self.assertLessEqual(plan["stake"], 5.0 + 1e-9)

    def test_05_one_entry_per_crypto_window(self):
        bid = bet(self.st, ticker="KXBTC15M-W-00", event_ticker="KXBTC15M-W", series="KXBTC15M")
        self.st.close_bet(bid, 0.7, 1.0)             # sold early...
        plan, why = risk.decide(self.s, self.st, {"ticker": "KXBTC15M-W-00", "event_ticker": "KXBTC15M-W",
                                                  "series": "KXBTC15M"}, 0.8, 0.9,
                                {"yes": [(0.5, 100)], "no": [(0.45, 100)]})
        self.assertIsNone(plan)                      # ...no churn back into the same window
        self.assertIn("already traded", why)

    def test_06_pause_and_bench_expire(self):
        for i in range(50):
            bet(self.st, ticker=f"T{i}", event_ticker=f"E{i}")
        self.st.x("UPDATE bets SET status='graded', clv=-0.05, pnl=-0.1, graded_ts=?",
                  (int(time.time()),))
        self.assertTrue(guard.clv_tripped(self.st))
        old = int(time.time()) - (guard.RECOVERY_DAYS + 1) * 86400
        self.st.x("UPDATE bets SET graded_ts=?", (old,))
        self.assertFalse(guard.clv_tripped(self.st))  # the pause lifts on its own
        self.assertFalse(guard.league_benched(self.st, "KXNBAGAME"))

    def test_07_partial_exit_splits_position(self):
        bid = bet(self.st, contracts=10, stake=5, fee=0.2)
        sold = self.st.split_bet(bid, 4)
        self.st.close_bet(sold, 0.8, 4 * 0.3)
        rows = {r["id"]: r for r in self.st.q("SELECT * FROM bets")}
        self.assertEqual((rows[bid]["contracts"], rows[bid]["status"]), (6, "open"))
        self.assertEqual((rows[sold]["contracts"], rows[sold]["result"]), (4, "sold"))
        self.assertAlmostEqual(rows[bid]["stake"] + rows[sold]["stake"], 5)

    def test_08_feed_divergence_marks_data_stale(self):
        a = SymbolAnalytics("BTC/USD", OrderBook("BTC/USD"))
        now = time.time()
        a.book.snapshot([(100000, 1)], [(100010, 1)], now)
        a.add_reference("coinbase", now, 100005)
        self.assertFalse(a.summary(now)["data_quality"]["stale"])
        a.add_reference("coinbase", now, 101000)      # 100 bps apart: one feed is wrong
        self.assertTrue(a.summary(now)["data_quality"]["stale"])

    def test_09_calibration_is_per_domain(self):
        for i in range(80):
            bet(self.st, ticker=f"S{i}", event_ticker=f"S{i}", model_prob=0.75)
        self.st.x("UPDATE bets SET status='graded', result='no'")   # sports badly over-confident
        self.assertLess(brain.calibrate(0.75, self.st, "sports"), 0.7)
        self.assertEqual(brain.calibrate(0.75, self.st, "crypto"), 0.75)  # crypto untouched

    def test_10_bootstrap_detects_luck(self):
        import numpy as np
        rng = np.random.default_rng(1)
        y = rng.integers(0, 2, 400)
        g = np.arange(400)
        base = np.full(400, 0.5)
        noise = np.clip(0.5 + rng.normal(0, 0.02, 400), 0.01, 0.99)   # no real skill
        self.assertLess(ml.paired_bootstrap(noise, base, y, g), 0.9)
        skill = np.where(y == 1, 0.7, 0.3)                             # real skill
        self.assertGreater(ml.paired_bootstrap(skill, base, y, g), 0.99)

    def test_11_sim_charges_slippage_and_skips_wide_spreads(self):
        cols = ["kalshi_logit", "kalshi_spread"]
        import numpy as np
        X = np.array([[0.0, 0.02], [0.0, 0.20]])
        out = ml.sim_trades(np.array([0.60, 0.99]), X, cols, np.array([1, 1]),
                            np.array(["a", "b"]), thresholds=(0.03,))
        self.assertEqual(out[0.03]["trades"], 1)     # the 20-cent-wide quote is ignored


if __name__ == "__main__":
    unittest.main()


class TestAccountLimits(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()
        self.book = {"yes": [(0.40, 500)], "no": [(0.55, 500)]}   # YES ask $0.45

    def tearDown(self):
        self.d.cleanup()

    def test_max_open_positions(self):
        for i in range(self.s.max_open_positions):
            bet(self.st, ticker=f"T{i}", event_ticker=f"E{i}", stake=1)
        plan, why = risk.decide(self.s, self.st, {"ticker": "N", "event_ticker": "NEW"}, 0.7, 0.9, self.book)
        self.assertIsNone(plan)
        self.assertIn("positions open", why)

    def test_cash_never_negative(self):
        self.s.bankroll = 10.0
        bet(self.st, ticker="A", event_ticker="EA", stake=9.5, fee=0.2)
        plan, why = risk.decide(self.s, self.st, {"ticker": "N", "event_ticker": "NEW"}, 0.9, 0.9, self.book)
        self.assertIsNone(plan)                     # only $0.30 cash left
        acct = self.st.account(10.0)
        self.assertAlmostEqual(acct["cash"], 0.3)

    def test_daily_loss_breaker(self):
        bid = bet(self.st)
        self.st.close_bet(bid, 0.1, -31.0)
        ok, why = guard.status(self.st, 1000, daily_loss_limit=30)
        self.assertFalse(ok)
        self.assertIn("daily loss", why)

    def test_decision_audit_never_creates_fake_bets(self):
        self.st.add_decision_audit({
            "ts": 100, "ticker": "KXNBA-TEST", "event_ticker": "E1",
            "series": "KXNBAGAME", "decision": "rejected", "reason": "edge too small",
            "side": "yes", "market_price": 0.55, "model_prob": 0.54,
            "side_prob": 0.54, "edge": -0.01, "confidence": 0.7,
            "signals": {"news": {"prob": 0.6}},
        })
        self.assertEqual(self.st.q("SELECT COUNT(*) AS n FROM bets")[0]["n"], 0)
        self.assertEqual(self.st.q("SELECT COUNT(*) AS n FROM decision_audit")[0]["n"], 1)

    def test_decision_audit_grades_only_after_settlement_and_labels_diagnostic_pnl(self):
        self.st.add_decision_audit({
            "ts": 100, "ticker": "KXNBA-TEST", "event_ticker": "E1",
            "series": "KXNBAGAME", "decision": "candidate", "reason": "risk passed",
            "side": "yes", "market_price": 0.4, "model_prob": 0.7,
            "side_prob": 0.7, "edge": 0.3, "confidence": 0.8,
        })
        self.st.add_decision_audit({
            "ts": 101, "ticker": "KXNBA-TEST", "event_ticker": "E1",
            "series": "KXNBAGAME", "decision": "rejected", "reason": "manual filter",
            "side": "yes", "market_price": 0.4, "model_prob": 0.6,
            "side_prob": 0.6, "edge": 0.2, "confidence": 0.7,
        })
        self.assertEqual(self.st.q("SELECT COUNT(*) AS n FROM decision_audit WHERE outcome IS NOT NULL")[0]["n"], 0)
        self.st.grade_decision_audit("KXNBA-TEST", 1, 200)
        rows = self.st.q("SELECT decision, outcome, brier, hypothetical_pnl FROM decision_audit ORDER BY id")
        self.assertEqual([r["outcome"] for r in rows], [1, 1])
        self.assertAlmostEqual(rows[0]["brier"], 0.09)
        self.assertAlmostEqual(rows[0]["hypothetical_pnl"], 0.6)
        self.assertIsNone(rows[1]["hypothetical_pnl"])
        self.assertEqual(self.st.q("SELECT COUNT(*) AS n FROM bets")[0]["n"], 0)
