"""Read from 20 s before the start, bet from 90 s; the last 20 s of every window; and the hindsight
review: after a window settles, which setup would have worked, and which of those
setups have earned a place in the playbook."""
import json
import math
import os
import random
import tempfile
import time
import unittest
from unittest.mock import patch

from bot import brain, hindsight, history, window_review
from bot.config import Settings
from bot.live.runner import LiveRunner
from bot.signals.base import Reading
from bot.storage import Store


def feats(**kw):
    f = {"kalshi_logit": 0.0, "kalshi_spread": 0.02, "model_prob": 0.6, "model_minus_kalshi": 0.1,
         "adx_1m": 30.0, "ema_9_21": 0.4, "ret_5m": 0.8, "ret_15m": 0.2, "macd_hist": 0.3, "rsi_15m": 55.0,
         "bb_pctb": 0.6, "vwap_dist": 0.5, "vol_spike_5m": 1.8, "obv_slope_15m": 0.3, "dist_sigma": 0.4,
         "vol_ratio_15_60": 1.4}
    f.update(kw)
    return f


class TestTiming(unittest.TestCase):
    def test_read_from_20_seconds_before_the_start_and_bet_from_90(self):
        s = Settings(mode="paper")
        self.assertEqual((s.crypto_read_seconds, s.crypto_entry_delay_seconds), (-20.0, 90.0))
        self.assertEqual(s.crypto_read_every_seconds, 10.0)
        self.assertEqual(s.crypto_max_minutes, 13.5)               # 15 min - 90 s
        with patch.dict(os.environ, {"CRYPTO_ENTRY_DELAY_SECONDS": "120"}):
            self.assertEqual(Settings(mode="paper").crypto_max_minutes, 13.0)

    def test_the_read_window_is_20_seconds_before_the_start_to_90_seconds_in(self):
        r = LiveRunner.__new__(LiveRunner)
        r.s = Settings(mode="paper")
        close = 10_000
        for elapsed, due in ((-20.1, False), (-20.0, True), (0.0, True), (45.0, True), (89.9, True), (90.0, False)):
            self.assertEqual(r.read_due(close, close - 900 + elapsed), due, elapsed)

    def test_workflow_pins_the_timing(self):
        wf = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "bot.yml")).read()
        self.assertIn("CRYPTO_READ_SECONDS: '-20'", wf)
        self.assertIn("CRYPTO_ENTRY_DELAY_SECONDS: '90'", wf)


class TestRules(unittest.TestCase):
    def test_votes_regime_and_following_a_vote(self):
        v = hindsight.votes(feats())
        self.assertEqual((v["trend_follow"], v["macd"], v["momentum_5m"], v["strike_side"]), (1, 1, 1, 1))
        self.assertEqual(v["rsi_fade"], 0)                                      # no opinion at RSI 55
        self.assertEqual(hindsight.votes(feats(rsi_15m=75))["rsi_fade"], -1)
        self.assertEqual(hindsight.votes(feats(adx_1m=15))["trend_follow"], 0)  # choppy: trend rules abstain
        self.assertEqual(hindsight.votes({"kalshi_logit": 0.0})["book_pressure"], 0)  # live-only inputs missing
        self.assertEqual(hindsight.regime(feats()), "trend/volup")
        self.assertEqual(hindsight.regime(feats(adx_1m=15, vol_ratio_15_60=0.7)), "range/voldown")
        # YES at 50c, 1.75c fee: wins +0.4825, loses -0.5175; NO at 50c is symmetric
        self.assertAlmostEqual(hindsight.follow_pnl(1, 0.5, 0.0, 1), 0.4825)
        self.assertAlmostEqual(hindsight.follow_pnl(1, 0.5, 0.0, 0), -0.5175)
        self.assertAlmostEqual(hindsight.follow_pnl(-1, 0.5, 0.0, 0), 0.4825)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.now = int(time.time())

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def read(self, ticker="KXBTC15M-T1", strike=80500.0, late=False, f=None, elapsed=45):
        close = self.now + 900 - elapsed
        terms = {"strike": strike, "close_ts": close, "avg_seconds": 60}
        readings = [Reading("kalshi_market", 0.50, 0.9, {}), Reading("crypto_model", 0.62, 0.75, {}),
                    Reading("crypto_momentum", 0.40, 0.3, {})]
        ctx = {"kalshi_yes_bid": 0.49, "kalshi_yes_ask": 0.51, "spot": {"composite": 80620.0},
               "features": f or feats(), "chart_setups": {"setups": []}, "market_context": {}}
        return window_review.record_open(self.st, ticker, {"series": "KXBTC15M"}, terms, self.now, readings, ctx,
                                         brain.DEFAULT_WEIGHTS, late=late, playbook={"rules": ["macd"]})


class TestClosingTape(Base):
    def test_tape_stats_and_text(self):
        t0 = 1_000.0
        pts = [(t0 + i, 80_500 * (1 + (i - 10) * 1e-5)) for i in range(21)]   # drifting up through the target
        st = window_review.tape_stats(pts, 80_500.0)
        self.assertTrue(st["crossed"])
        self.assertEqual(st["side"], "YES")
        self.assertGreater(st["change_bps"], 0)
        self.assertIn("crossed the target", window_review.closing_text("BTC", st))
        self.assertIsNone(window_review.tape_stats(pts[:2], 80_500.0))        # too little to say
        flat = [(t0 + i, 80_600.0) for i in range(10)]
        self.assertIn("never crossed", window_review.closing_text("BTC", window_review.tape_stats(flat, 80_500.0)))

    def test_closing_read_attaches_to_the_window_and_the_verdict_uses_it(self):
        self.read()
        pts = [(self.now + i, 80_300.0 - i) for i in range(20)]               # ends below the 80,500 target
        self.assertIsNotNone(window_review.record_close(self.st, "KXBTC15M-T1", 80_500.0, pts))
        window_review.settle(self.st, "KXBTC15M-T1", 0)
        r = self.st.q("SELECT * FROM window_log")[0]
        self.assertIn("Last 19s", r["closing_text"])
        self.assertIn("closing tape pointed NO, as it settled", r["note"])
        self.assertIsNone(window_review.record_close(self.st, "KXNOPE", 1.0, pts))   # no read, nothing to attach to

    def test_runner_keeps_the_last_20_seconds_then_reviews_them_at_the_close(self):
        r = LiveRunner.__new__(LiveRunner)
        r.store, r.tape, r.window_read = self.st, {}, {}
        close = self.now + 30

        class A:
            def __init__(self):
                self.px = 80_400.0

            def composite_price(self, now):
                return self.px

        a = A()
        r.coins = {"KXBTC15M": a}
        r.markets = {"KXBTC15M-T1": {"series": "KXBTC15M", "terms": {"close_ts": close, "strike": 80_500.0}}}
        self.read(strike=80_500.0)
        for i, sec_left in enumerate(range(25, 0, -1)):                        # one tick a second
            a.px = 80_400.0 + i * 8
            r.closing_tapes(close - sec_left)
        self.assertEqual(len(r.tape["KXBTC15M-T1"]["pts"]), 20)                # nothing kept before the last 20 s
        r.closing_tapes(close + 1)                                             # the window has closed
        self.assertEqual(r.tape, {})
        self.assertTrue(self.st.q("SELECT closing_text FROM window_log")[0]["closing_text"])


class TestHindsightReview(Base):
    def test_late_reads_are_marked(self):
        row = self.read(late=True, elapsed=100)
        self.assertTrue(json.loads(row["snapshot"])["late"])
        self.assertEqual(json.loads(row["snapshot"])["elapsed_s"], 100)

    def test_every_rule_is_scored_on_the_window_and_the_setup_is_reviewed(self):
        self.read(f=feats(rsi_15m=75))
        self.st.x("""INSERT INTO bets(ts,mode,ticker,side,price,contracts,stake,fee,status,pnl)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""", (self.now, "paper", "KXBTC15M-T1", "yes", 0.54, 20, 10.8, 0.2, "graded", -11.0))
        window_review.settle(self.st, "KXBTC15M-T1", 0)                          # NO won
        text = hindsight.review_window(self.st, "KXBTC15M-T1", 0)
        self.assertIn("NO won", text)
        self.assertIn("Bot bet YES at 54c and lost -11.00", text)
        self.assertIn("backed by", text)
        self.assertIn("Would have worked: ", text)                               # something called NO
        self.assertIn("Would not have: ", text)
        self.assertIn("Trend follow", text.split("Would not have:")[1])          # it leaned YES, so it failed
        rows = {r["rule"]: r for r in self.st.q("SELECT * FROM hindsight_rules")}
        self.assertEqual(rows["trend_follow"]["correct"], 0)
        self.assertLess(rows["trend_follow"]["pnl"], 0)
        self.assertEqual(rows["trend_follow"]["regime"], "trend/volup")
        self.assertIn("RSI", text.split("Would not have:")[0])                   # the fade called NO and was right
        self.assertNotIn("book_pressure", rows)                                  # abstained: not scored
        self.assertEqual(self.st.q("SELECT hindsight FROM window_log")[0]["hindsight"], text)
        hindsight.review_window(self.st, "KXBTC15M-T1", 0)                       # once per window
        self.assertEqual(self.st.q("SELECT COUNT(*) c FROM hindsight_rules WHERE rule='trend_follow'")[0]["c"], 1)

    def test_a_window_without_a_bet_says_so(self):
        self.read()
        window_review.settle(self.st, "KXBTC15M-T1", 1)
        self.assertIn("Bot did not bet", hindsight.review_window(self.st, "KXBTC15M-T1", 1))

    def test_history_replays_score_the_toolbox_once_per_window(self):
        rnd = random.Random(5)
        close = 1_800_000_000
        px, spot_list = 80_000.0, []
        for i in range(260):
            px *= math.exp(rnd.gauss(0, 4e-4))
            ts = close - 60 * (260 - i)
            spot_list.append({"ts": ts, "open": px, "high": px * 1.0003, "low": px * 0.9997, "close": px, "volume": 5 + rnd.random()})
        spot = {c["ts"]: c["close"] for c in spot_list}
        m = {"ticker": "KXBTC15M-H1", "floor_strike": spot_list[-16]["close"], "result": "yes",
             "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(close))}
        candles = [{"end_period_ts": close - 60 * k, "yes_bid": {"close": 48}, "yes_ask": {"close": 50}} for k in range(13, 2, -1)]
        history.replay_market(m, candles, spot, 0.07, spot_list=spot_list, store=self.st)
        n = self.st.q("SELECT COUNT(DISTINCT ticker) c, COUNT(*) r FROM hindsight_rules")[0]
        self.assertEqual(n["c"], 1)                                              # one scored read per window, not per minute
        self.assertGreater(n["r"], 3)


class TestPlaybook(Base):
    def fill(self, rule, regime, wins, losses, mid=0.5):
        for i in range(wins + losses):
            self.st.x("INSERT INTO hindsight_rules VALUES(?,?,?,?,?,?,?,?)",
                      (f"{rule}-{regime}-{i}", rule, "BTC", self.now, regime, 1, int(i < wins),
                       hindsight.follow_pnl(1, mid, 0.0, 1 if i < wins else 0)))

    def test_only_rules_that_stay_profitable_after_fees_are_validated(self):
        hindsight.ensure_table(self.st)
        self.fill("trend_follow", "trend/volup", 78, 22)      # 78% on 100 windows: a real edge
        self.fill("macd", "trend/volup", 52, 48)              # a coin flip: fees make it a loser
        self.fill("rsi_fade", "range/voldown", 14, 6)         # 70% but only 20 windows: too few to trust
        t = hindsight.playbook(self.st)["rules"]
        self.assertTrue(t["trend/volup|trend_follow"]["validated"])
        self.assertFalse(t["trend/volup|macd"]["validated"])
        self.assertFalse(t["range/voldown|rsi_fade"]["validated"])
        self.assertTrue(t["all|trend_follow"]["validated"])                      # also on its overall record

    def test_a_validated_setup_becomes_one_signal_for_the_current_condition(self):
        hindsight.ensure_table(self.st)
        self.fill("trend_follow", "trend/volup", 78, 22)
        table = hindsight.playbook(self.st)
        r = hindsight.playbook_reading(table, feats())                           # trending, volatility rising, EMA up
        self.assertEqual(r.name, "crypto_playbook")
        self.assertGreater(r.prob, 0.5)                                          # leans YES, with Kalshi at 50%
        self.assertLessEqual(r.prob, 0.5 + 0.06)                                 # capped: one setup never dominates
        self.assertEqual(r.detail["rules"], ["trend_follow"])
        self.assertLessEqual(r.confidence, 0.6)
        self.assertIsNone(hindsight.playbook_reading(table, feats(adx_1m=10)))    # trend rule abstains in chop
        self.assertIsNone(hindsight.playbook_reading(hindsight.playbook(Store(os.path.join(self.d.name, "e.db"))), feats()))

    def test_summary_for_the_app_and_the_signal_starts_small(self):
        hindsight.ensure_table(self.st)
        self.fill("trend_follow", "trend/volup", 78, 22)
        s = hindsight.playbook_summary(self.st)
        self.assertEqual((s["windows"], s["min_windows"]), (100, 50))
        self.assertEqual(s["top"][0]["label"], "Trend follow")
        self.assertGreater(s["top"][0]["edge"], 0)
        self.assertEqual(brain.DEFAULT_WEIGHTS["crypto_playbook"], 0.15)         # earns more weight only by beating Kalshi

    def test_the_runner_adds_the_playbook_signal_and_reads_it_into_the_window(self):
        hindsight.ensure_table(self.st)
        self.fill("trend_follow", "trend/volup", 78, 22)
        r = LiveRunner.__new__(LiveRunner)
        r.store, r._pb, r.window_read = self.st, (0.0, None), {}
        table = r.playbook_table(self.now)
        self.assertTrue(table["rules"]["trend/volup|trend_follow"]["validated"])
        pb = hindsight.playbook_reading(table, feats())
        terms = {"strike": 80_500.0, "close_ts": self.now + 855, "avg_seconds": 60}
        readings = [Reading("kalshi_market", 0.50, 0.9, {}), pb]
        ctx = {"kalshi_yes_bid": 0.49, "kalshi_yes_ask": 0.51, "spot": {}, "features": feats()}
        r.s = Settings(mode="paper")
        t = {"series": "KXBTC15M"}
        r.take_window_read("KXBTC15M-T1", t, terms, self.now, readings, ctx, brain.DEFAULT_WEIGHTS)
        r.take_window_read("KXBTC15M-T1", t, terms, self.now + 3, readings, ctx, brain.DEFAULT_WEIGHTS)   # too soon: skipped
        r.take_window_read("KXBTC15M-T1", t, terms, self.now + 12, readings, ctx, brain.DEFAULT_WEIGHTS)  # keeps reading
        r.take_window_read("KXBTC15M-T1", t, terms, self.now + 24, readings, ctx, brain.DEFAULT_WEIGHTS)
        trail = json.loads(self.st.q("SELECT snapshot FROM window_log")[0]["snapshot"])["reads"]
        self.assertEqual(len(trail), 3)                                            # one step per read, newest last
        self.assertEqual(self.st.q("SELECT read_ts FROM window_log")[0]["read_ts"], self.now + 24)
        snap = json.loads(self.st.q("SELECT snapshot FROM window_log")[0]["snapshot"])
        self.assertEqual(snap["playbook"]["rules"], ["trend_follow"])             # the read records what the playbook said
        self.assertEqual(self.st.q("SELECT COUNT(*) c FROM window_log")[0]["c"], 1)


if __name__ == "__main__":
    unittest.main()
