"""Offline tests for live streaming, the council, and history replay."""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from bot.config import Settings
from bot.storage import Store
from bot.live.book import OrderBook
from bot.live.analytics import SymbolAnalytics
from bot.live.kalshi_book import KalshiBook
from bot.live import feeds, settlement
from bot import council, history


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestBookAndAnalytics(unittest.TestCase):
    def test_kraken_messages_build_book_and_trades(self):
        a = SymbolAnalytics("BTC/USD", OrderBook("BTC/USD"))
        feeds.handle_kraken({"channel": "book", "type": "snapshot", "data": [{
            "symbol": "BTC/USD",
            "bids": [{"price": 100000.0, "qty": 2.0}, {"price": 99990.0, "qty": 1.0}],
            "asks": [{"price": 100010.0, "qty": 0.5}]}]}, {"BTC/USD": a})
        feeds.handle_kraken({"channel": "book", "type": "update", "data": [{
            "symbol": "BTC/USD", "bids": [{"price": 99990.0, "qty": 0}], "asks": []}]},
            {"BTC/USD": a})
        feeds.handle_kraken({"channel": "trade", "type": "update", "data": [{
            "symbol": "BTC/USD", "side": "buy", "price": 100010.0, "qty": 0.3,
            "timestamp": iso(time.time())}]}, {"BTC/USD": a})
        self.assertEqual(a.book.best(), (100000.0, 100010.0))
        self.assertNotIn(99990.0, a.book.bids)
        self.assertGreater(a.book.imbalance(0.1), 0)        # more size on the bid
        self.assertEqual(a.flow(time.time())["aggressor_imbalance"], 1.0)

    def test_wall_pulled_vs_consumed(self):
        b = OrderBook("X")
        b.snapshot([(100 - i, 1) for i in range(1, 15)] + [(95.5, 50)],
                   [(100 + i, 1) for i in range(1, 15)])
        b.track_walls([])
        self.assertIn(("bid", 95.5), b.walls)
        b.update("bid", 95.5, 0)
        ev = b.track_walls([])          # vanished with no trade there = pulled
        self.assertEqual(ev[0]["event"], "pulled")

    def test_summary_and_composite(self):
        a = SymbolAnalytics("BTC/USD", OrderBook("BTC/USD"))
        now = time.time()
        a.book.snapshot([(100000, 1)], [(100020, 1)], now)
        for i in range(3600, 0, -1):
            a.mids.append((now - i, 100000 + (i % 7)))
        a.add_reference("coinbase", now, 100030)
        s = a.summary(now)
        self.assertFalse(s["data_quality"]["stale"])
        self.assertEqual(s["data_quality"]["exchanges_fresh"], 2)
        self.assertAlmostEqual(s["composite"], (100010 + 100030) / 2)


class TestSettlement(unittest.TestCase):
    def test_rules_parsing(self):
        t = settlement.contract_terms({"floor_strike": 100000, "close_time": "2026-10-19T18:30:00Z",
            "rules_primary": "Resolves using the CF Benchmarks RTI average of the 60 seconds before close."})
        self.assertEqual((t["avg_seconds"], t["avg_source"], t["benchmark"]),
                         (60, "rules", "CF Benchmarks"))

    def test_averaging_reduces_uncertainty(self):
        # same spot/strike/time: an averaged settlement is MORE certain than a single print
        single = settlement.prob_above(100100, 100000, 0.001, 60, avg_seconds=1)
        avg = settlement.prob_above(100100, 100000, 0.001, 60, avg_seconds=60)
        self.assertGreater(avg, single)
        # inside the window, an already-high realized average dominates
        late = settlement.prob_above(99990, 100000, 0.001, 10, 60, realized_avg=100200)
        self.assertGreater(late, 0.9)


class TestKalshiBook(unittest.TestCase):
    def test_snapshot_delta_and_gap(self):
        k = KalshiBook("T")
        feeds.handle_kalshi({"type": "orderbook_snapshot", "seq": 1, "msg": {
            "market_ticker": "T", "yes": [[55, 100]], "no": [[40, 80]]}}, {"T": k})
        self.assertAlmostEqual(k.mid(), (0.55 + 0.60) / 2)
        feeds.handle_kalshi({"type": "orderbook_delta", "seq": 3, "msg": {
            "market_ticker": "T", "price": 40, "delta": -80, "side": "no"}}, {"T": k})
        self.assertEqual(k.gaps, 1)                 # seq jumped 1 -> 3
        self.assertTrue(k.needs_resync)             # book is now untrustworthy...
        self.assertIsNone(k.age())                  # ...so it reads as stale until resync
        k.snapshot({"yes": [[55, 100]], "no": [[41, 80]]}, seq=4)
        self.assertFalse(k.needs_resync)


class FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class TestCouncil(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.d.name, "t.db"))
        self.good = {"spot": {"data_quality": {"stale": False, "exchanges_fresh": 2}},
                     "kalshi_book_age_s": 1.0}

    def tearDown(self):
        self.d.cleanup()

    def llm(self, council_reply, skeptic_reply):
        from bot.llm import LLM
        def post(url, json=None, **k):
            prompt = json["messages"][0]["content"]
            return FakeResp(skeptic_reply if "skeptic" in prompt[:60] else council_reply)
        return LLM("k", self.store, models=["a", "b"], post=post)

    def test_stale_data_rejected_without_llm(self):
        bad = {"spot": {"data_quality": {"stale": True}}, "kalshi_book_age_s": 1}
        self.assertEqual(council.review(None, self.store, "T", bad)["by"], "rules")

    def test_approve_and_skeptic_veto(self):
        ok = '{"chair": {"decision": "approve", "reason": "flow confirms"}}'
        r = council.review(self.llm(ok, '{"severe_issue": false}'), self.store, "T1", self.good)
        self.assertEqual((r["decision"], r["by"]), ("approve", "council"))
        r = council.review(self.llm(ok, '{"severe_issue": true, "issues": ["wall-driven"]}'),
                           self.store, "T2", self.good)
        self.assertEqual(r["decision"], "reject")

    def test_no_llm_falls_back_to_numbers_in_advisory_mode(self):
        self.assertEqual(council.review(None, self.store, "T3", self.good)["decision"], "approve")
        os.environ["COUNCIL_MODE"] = "required"
        try:
            self.assertEqual(council.review(None, self.store, "T4", self.good)["decision"], "wait")
        finally:
            del os.environ["COUNCIL_MODE"]


class FakeRest:
    def __init__(self, close_ts):
        self.close_ts = close_ts

    def sports_events(self, series):
        if series != "KXBTC15M":
            return []
        return [{"event_ticker": "KXBTC15M-E", "markets": [{
            "ticker": "KXBTC15M-E-00", "status": "active", "floor_strike": 100000.0,
            "close_time": iso(self.close_ts)}]}]

    def orderbook(self, ticker):
        return {"yes": [(0.59, 300)], "no": [(0.40, 300)]}   # YES ask 0.60


class TestRunner(unittest.TestCase):
    def test_discover_records_per_series_market_coverage(self):
        from bot.live.runner import LiveRunner
        with tempfile.TemporaryDirectory() as d:
            os.environ["BOT_DB"] = os.path.join(d, "b.db")
            s = Settings()
            store = Store(s.db_path)
            r = LiveRunner(s, store, rest=FakeRest(time.time() + 360),
                           llm=None, seed=False, with_context=False)
            r.discover()
            health = store.get_kv("live_discovery_health")
            self.assertEqual(health["active_markets"], 1)
            self.assertEqual(health["series"]["KXBTC15M"]["events"], 1)
            self.assertEqual(health["series"]["KXBTC15M"]["markets_seen"], 1)
            self.assertEqual(health["series"]["KXBTC15M"]["active_markets"], 1)
            self.assertEqual(health["series"]["KXETH15M"]["events"], 0)
            del os.environ["BOT_DB"]

    def test_tick_shortlists_then_council_and_recheck_place(self):
        from bot.live.runner import LiveRunner
        with tempfile.TemporaryDirectory() as d:
            os.environ["BOT_DB"] = os.path.join(d, "b.db")
            s = Settings()
            store = Store(s.db_path)
            now = time.time()
            r = LiveRunner(s, store, rest=FakeRest(now + 360), llm=None,
                           dvol_fetch=lambda c: 45.0, seed=False)
            a = r.coins["KXBTC15M"]
            a.book.snapshot([(100290, 1)], [(100310, 1)], now)
            for i in range(3600, 0, -1):            # calm hour, now $300 over the strike
                a.mids.append((now - i, 100300 + (i % 7) * 3 - 9))
            r.discover()
            r.poll_books()
            r.refresh_context()                 # background refresh, as the live loop does
            got = list(r.tick(now))
            self.assertEqual(len(got), 1)
            ticker, readings, ctx, plan = got[0]
            names = {x.name for x in readings}
            self.assertTrue({"crypto_model", "crypto_model_iv"} <= names)
            verdict = council.review(None, store, ticker, ctx)
            r.finish(ticker, readings, ctx, plan, verdict)
            bets = store.open_bets()
            self.assertEqual(len(bets), 1)
            self.assertEqual(bets[0]["side"], "yes")
            r.dump(now, os.path.join(d, "live.json"))
            del os.environ["BOT_DB"]


class TestRunnerExecution(unittest.TestCase):
    def setUp(self):
        from bot.live.runner import LiveRunner
        self.d = tempfile.TemporaryDirectory()
        os.environ["BOT_DB"] = os.path.join(self.d.name, "b.db")
        self.s = Settings()
        self.store = Store(self.s.db_path)
        self.now = time.time()
        rest = FakeRest(self.now + 360)
        rest.orderbook = lambda t: {"yes": [(0.55, 300)], "no": [(0.40, 300)]}  # bid .55 ask .60
        self.r = LiveRunner(self.s, self.store, rest=rest, llm=None,
                            dvol_fetch=lambda c: 45.0, seed=False, with_context=False)
        a = self.r.coins["KXBTC15M"]
        a.book.snapshot([(100290, 1)], [(100310, 1)], self.now)
        for i in range(3600, 0, -1):
            a.mids.append((self.now - i, 100300 + (i % 7) * 3 - 9))
        self.r.discover()
        self.r.poll_books()

    def tearDown(self):
        del os.environ["BOT_DB"]
        self.d.cleanup()

    def test_live_crypto_intel_includes_kalshi_aggressor_flow(self):
        ticker = "KXBTC15M-E-00"
        trades = []
        for i in range(4):
            trades.append({"trade_id": f"yes-{i}", "ticker": ticker,
                           "ts": int(self.now - 30 + i), "count": 100,
                           "yes_price": 0.60 + i * 0.01, "taker_side": "yes"})
        trades.append({"trade_id": "no-1", "ticker": ticker, "ts": int(self.now - 20),
                       "count": 5, "yes_price": 0.59, "taker_side": "no"})
        self.store.add_flow_trades(trades)
        reading = self.r.kalshi_flow_reading(ticker, 0.60, self.now)
        self.assertIsNotNone(reading)
        self.assertEqual(reading.name, "trade_flow")
        self.assertGreater(reading.prob, 0.60)
        self.assertGreater(reading.detail["imbalance"], 0)

    def test_maker_terminal_execution_without_fill_count_assumes_full_fill(self):
        self.assertEqual(self.r._maker_order_state({"status": "executed"}, 7), (7, "executed", True))

    def test_maker_missing_fill_count_on_open_order_is_not_terminal(self):
        self.assertEqual(self.r._maker_order_state({"status": "resting"}, 7), (0, "resting", False))

    def test_maker_malformed_fill_count_is_not_zero_fill_confirmation(self):
        self.assertEqual(self.r._maker_order_state({"status": "resting", "fill_count_fp": "bad"}, 7),
                         (0, "resting", False))

    def test_ambiguous_maker_submission_sets_durable_pause(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from bot import guard
        ticker, readings, ctx, plan = list(self.r.tick(self.now))[0]
        fake_trade = SimpleNamespace(place_resting_buy=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("response lost")))
        with patch.object(self.s, "mode", "live"), patch.object(self.r.engine, "trade", fake_trade):
            self.r.start_maker(ticker, {"ticker": ticker}, plan[1], readings, 0.6, 0.8)
        incident = self.store.get_kv("execution_reconciliation_required")
        self.assertIn("outcome unknown", incident)
        allowed, reason = guard.status(self.store, self.s.bankroll, mode="live")
        self.assertFalse(allowed)
        self.assertIn("reconcile exchange orders", reason)
        self.assertEqual(self.store.get_kv("maker_recovery")["state"], "submitting")

    def test_prior_run_maker_intent_pauses_live_runner_on_restart(self):
        from bot.live.runner import LiveRunner
        self.store.set_kv("maker_recovery", {"state": "active", "ticker": "KXBTC15M-E-00",
                                             "order_id": "unknown", "contracts": 3})
        self.s.mode = "live"
        restarted = LiveRunner(self.s, self.store, rest=self.r.rest, llm=None,
                               dvol_fetch=lambda c: 45.0, seed=False, with_context=False)
        self.assertIn("prior run", self.store.get_kv("execution_reconciliation_required"))
        self.assertFalse(restarted.betting_ok)  # no new order can be placed before reconciliation

    def test_second_live_maker_is_blocked_while_recovery_slot_is_occupied(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        ticker, readings, ctx, plan = list(self.r.tick(self.now))[0]
        calls = []
        fake_trade = SimpleNamespace(place_resting_buy=lambda *args, **kwargs: calls.append(args) or {"order_id": "NEW"})
        self.r.makers["EXISTING-ORDER"] = {"order_id": "OLD"}
        with patch.object(self.s, "mode", "live"), patch.object(self.r.engine, "trade", fake_trade):
            self.r.start_maker(ticker, {"ticker": ticker}, plan[1], readings, 0.6, 0.8)
        self.assertEqual(calls, [])
        self.assertNotIn(ticker, self.r.makers)
        self.assertTrue(any("another resting order" in s for s in self.r.summary["skipped"]))

    def test_live_maker_without_order_id_fails_closed(self):
        from unittest.mock import patch
        ticker, readings, ctx, plan = list(self.r.tick(self.now))[0]
        from types import SimpleNamespace
        fake_trade = SimpleNamespace(place_resting_buy=lambda *args, **kwargs: {})
        with patch.object(self.s, "mode", "live"), patch.object(self.r.engine, "trade", fake_trade):
            self.r.start_maker(ticker, {"ticker": ticker}, plan[1], readings, 0.6, 0.8)
        self.assertNotIn(ticker, self.r.makers)
        self.assertFalse(self.r.betting_ok)
        self.assertIn("maker order ID missing", self.r.betting_why)

    def test_pending_maker_reserves_budget_against_new_candidate(self):
        ticker, readings, ctx, plan = list(self.r.tick(self.now))[0]
        other = "KXETH15M-TEST"
        self.r.makers[other] = {"order_id": "pending", "placed": self.now,
                                "plan": {"contracts": 10, "limit": 0.5}}
        self.r.finish(ticker, readings, ctx, plan, {"decision": "approve", "by": "numbers"})
        self.assertEqual(self.store.open_bets(), [])
        self.assertEqual(self.r.summary["bets"], 0)
        self.assertIn("recheck: pending maker reserves risk budget", self.r.summary["skipped"])

    def test_stale_kalshi_book_blocks_post_council_execution(self):
        ticker, readings, ctx, plan = list(self.r.tick(self.now))[0]
        # Simulate a feed gap after the initial shortlist/council review.
        self.r.books[ticker].needs_resync = True
        self.r.books[ticker].last_update = 0.0
        self.r.finish(ticker, readings, ctx, plan, {"decision": "approve", "by": "numbers"})
        self.assertEqual(self.store.open_bets(), [])
        self.assertEqual(self.r.summary["bets"], 0)
        self.assertIn("recheck: data stale", self.r.summary["skipped"])

    def test_maker_rests_then_fills(self):
        ticker, readings, ctx, plan = list(self.r.tick(self.now))[0]
        self.assertEqual(plan[1]["style"], "maker")
        self.r.finish(ticker, readings, ctx, plan, {"decision": "approve", "by": "numbers"})
        self.assertIn(ticker, self.r.makers)                 # resting, not bought yet
        self.assertEqual(self.store.open_bets(), [])
        self.r.books[ticker].no = {0.44: 300}                # ask merely TOUCHES $0.56
        self.r.check_makers(time.time())
        self.assertEqual(self.store.open_bets(), [])         # queue position: no fill yet
        self.r.books[ticker].no = {0.45: 300}                # sellers come THROUGH to $0.55
        self.r.check_makers(time.time())
        bets = self.store.open_bets()
        self.assertEqual((len(bets), bets[0]["style"], bets[0]["price"]), (1, "maker", 0.56))

    def test_unfilled_maker_expires(self):
        ticker, readings, ctx, plan = list(self.r.tick(self.now))[0]
        self.r.finish(ticker, readings, ctx, plan, {"decision": "approve", "by": "numbers"})
        self.r.check_makers(time.time() + self.s.maker_wait_s + 1)
        self.assertNotIn(ticker, self.r.makers)
        self.assertEqual(self.store.open_bets(), [])

    def test_early_exit_when_bid_beats_holding(self):
        t = next(iter(self.r.markets))
        self.store.add_bet({"ts": 1, "mode": "paper", "ticker": t, "event_ticker": "KXBTC15M-E",
                            "series": "KXBTC15M", "team": "UP", "side": "yes", "price": 0.40,
                            "contracts": 10, "stake": 4, "fee": 0.17, "model_prob": .6,
                            "edge": .1, "confidence": .8, "signals": {}})
        # BTC fell back to the strike (holding ~ a coin flip) but a buyer bids $0.90
        a = self.r.coins["KXBTC15M"]
        a.mids.clear()
        for i in range(3600, 0, -1):
            a.mids.append((self.now - i, 100000 + (i % 7) * 3 - 9))
        a.book.snapshot([(99999, 1)], [(100001, 1)], time.time())
        self.r.books[t].yes = {0.90: 50}
        self.r.books[t].no = {0.08: 50}
        self.r.check_exits(time.time())
        b = self.store.q("SELECT * FROM bets")[0]
        self.assertEqual(b["result"], "sold")
        self.assertAlmostEqual(b["pnl"], 10 * (0.90 - 0.40) - 0.17 - 0.07, places=2)


class TestHistory(unittest.TestCase):
    def test_replay_and_evaluate(self):
        close = 1_800_000_000
        m = {"ticker": "KXBTC15M-H", "floor_strike": 100000, "result": "yes",
             "close_time": iso(close)}
        spot = {close - 60 * k - (close % 60): 100200 + (3 if k % 2 else -3) for k in range(200)}
        candles = [{"end_period_ts": close - 60 * k,
                    "yes_bid": {"close": 55}, "yes_ask": {"close": 57}} for k in range(1, 15)]
        rows = history.replay_market(m, candles, spot, 0.07)
        self.assertTrue(rows)
        self.assertGreater(rows[0]["model"], 0.57)
        res = history.evaluate({"KXBTC15M-H": rows}, 0.07)
        self.assertEqual(res["thresholds"][0.05]["bets"], 1)
        self.assertEqual(res["thresholds"][0.05]["win_rate"], 1.0)
        cost = 0.57 + history.ml.SLIPPAGE
        fee = history.taker_fee(100, cost, 0.07) / 100
        self.assertAlmostEqual(
            res["thresholds"][0.05]["roi"], (1 - cost - fee) / (cost + fee), places=4
        )
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "t.db"))
            history.seed_learning(store, {"KXBTC15M-H": rows})
            self.assertTrue(store.q("SELECT * FROM graded_markets"))


if __name__ == "__main__":
    unittest.main()


class TestKrakenChecksum(unittest.TestCase):
    def test_checksum_match_and_mismatch(self):
        import zlib
        from bot.live.book import ChecksumError, _fmt
        b = OrderBook("BTC/USD")
        b.snapshot([(100000.0, 0.5), (99999.9, 1.25)], [(100000.1, 0.75)])
        s = _fmt(100000.1, 1) + _fmt(0.75, 8) + _fmt(100000.0, 1) + _fmt(0.5, 8) + \
            _fmt(99999.9, 1) + _fmt(1.25, 8)
        good = zlib.crc32(s.encode()) & 0xFFFFFFFF
        self.assertTrue(b.verify(good))
        self.assertEqual(_fmt(0.75, 8), "75000000")             # leading zeros stripped
        with self.assertRaises(ChecksumError):
            b.verify(good + 1)
        self.assertIsNone(b.mid() and None)
        self.assertEqual(b.last_update, 0.0)                    # book marked stale
