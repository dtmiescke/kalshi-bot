"""Offline tests: no network, fake Kalshi + sportsbook data.

    python -m unittest discover tests
"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from bot import backtest, brain, risk
from bot.config import Settings
from bot import dashboard
from bot.engine import Engine, _fresh_odds_history
from bot.grader import grade
from bot.odds import american_to_prob, consensus, devig_power
from bot.report import build
from bot.signals.base import Reading
from bot.signals.flow import flow_features
from bot.storage import Store


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeKalshi:
    """Celtics at 0.50 on Kalshi while sharp books say ~0.61 -> should bet YES."""
    def __init__(self, start):
        self.start = start
        self.result = None

    def sports_events(self, series):
        if series != "KXNBAGAME":
            return []
        return [{"event_ticker": "KXNBAGAME-26OCT20BOSNYK", "series_ticker": series,
                 "title": "Boston at New York", "markets": [
            {"ticker": "KXNBAGAME-26OCT20BOSNYK-BOS", "yes_sub_title": "Boston",
             "status": "active", "yes_bid": 49, "yes_ask": 50, "last_price": 50,
             "volume": 5000, "expected_expiration_time": iso(self.start + timedelta(hours=3))},
            {"ticker": "KXNBAGAME-26OCT20BOSNYK-NYK", "yes_sub_title": "New York",
             "status": "active", "yes_bid": 50, "yes_ask": 51, "last_price": 50,
             "volume": 5000, "expected_expiration_time": iso(self.start + timedelta(hours=3))},
        ]}]

    def trades(self, ticker, min_ts=None):
        now = int(time.time())
        return [{"trade_id": f"{ticker}-{i}", "created_time": iso(datetime.fromtimestamp(now - 60 * i, timezone.utc)),
                 "count": 120 if i % 5 == 0 else 10, "yes_price": 50,
                 "taker_side": "yes" if "BOS" in ticker else "no"} for i in range(30)]

    def orderbook(self, ticker, depth=20):
        if ticker.endswith("BOS"):   # YES ask 0.50 = NO bid 0.50
            return {"yes": [(0.49, 500)], "no": [(0.50, 400), (0.49, 400)]}
        return {"yes": [(0.50, 500)], "no": [(0.49, 400)]}

    def market(self, ticker):
        return {"result": self.result, "event_ticker": "KXNBAGAME-26OCT20BOSNYK"}


class FakeOdds:
    last_remaining = "499"

    def __init__(self, start):
        self.start = start

    def fetch(self, sport, regions="us"):
        if sport != "basketball_nba":
            return []
        def book(key, bos, nyk):
            return {"key": key, "markets": [{"key": "h2h", "outcomes": [
                {"name": "Boston Celtics", "price": bos}, {"name": "New York Knicks", "price": nyk}]}]}
        return [{"id": "evt1", "home_team": "New York Knicks", "away_team": "Boston Celtics",
                 "commence_time": iso(self.start), "bookmakers": [
            book("pinnacle", -170, 150), book("draftkings", -175, 145),
            book("fanduel", -165, 140), book("circasports", -170, 150),
            book("betmgm", -175, 145), book("caesars", -170, 140),
            book("betrivers", -168, 142), book("lowvig", -165, 150)]}]


class TestMath(unittest.TestCase):
    def test_american(self):
        self.assertAlmostEqual(american_to_prob(-150), 0.6)
        self.assertAlmostEqual(american_to_prob(150), 0.4)

    def test_devig_sums_to_one(self):
        p = devig_power([american_to_prob(-110), american_to_prob(-110)])
        self.assertAlmostEqual(sum(p), 1.0, places=6)
        self.assertAlmostEqual(p[0], 0.5, places=4)

    def test_fee(self):
        self.assertEqual(risk.taker_fee(100, 0.5, 0.07), 1.75)
        self.assertEqual(risk.taker_fee(1, 0.5, 0.07), 0.02)

    def test_asks_from_bids(self):
        a = risk.asks_from_book({"yes": [(0.54, 10)], "no": [(0.45, 10)]})
        self.assertEqual(a["yes"][0][0], 0.55)
        self.assertEqual(a["no"][0][0], 0.46)

    def test_calibration_uses_yes_probability_and_canonical_audits(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "calibration.sqlite"))
            for i in range(10):
                store.add_decision_audit({
                    "ts": 100 + i, "ticker": f"T{i}", "event_ticker": f"E{i}",
                    "series": "KXNBAGAME", "decision": "rejected", "reason": "test",
                    "side": "no", "market_price": 0.25, "model_prob": 0.75,
                    "side_prob": 0.25, "edge": 0.0, "confidence": 0.7,
                })
                store.grade_decision_audit(f"T{i}", 0, 1000, cutoff_ts=100 + i)
            table = brain.calibration_table(store, domain="sports")
            self.assertEqual(table[7]["n"], 10)
            self.assertAlmostEqual(table[7]["predicted"], 0.75)
            self.assertEqual(table[7]["actual"], 0.0)

    def test_blend_moves_toward_strong_signal(self):
        rs = [Reading("kalshi_market", 0.55, 1.0), Reading("book_consensus", 0.62, 1.0)]
        p, _ = brain.blend(rs, brain.DEFAULT_WEIGHTS)
        self.assertTrue(0.55 < p < 0.62)

    def test_high_win_rate_is_not_edge(self):
        """An 85% favourite priced at 0.86 must NOT be bet: wins often, loses money."""
        with tempfile.TemporaryDirectory() as d:
            s = Settings()
            s.db_path = os.path.join(d, "t.db")
            store = Store(s.db_path)
            book = {"yes": [(0.85, 100)], "no": [(0.14, 100)]}   # YES ask 0.86
            plan, why = risk.decide(s, store, {"ticker": "X", "event_ticker": "E"},
                                    0.85, 0.9, book)
            self.assertIsNone(plan)

    def test_flow_features(self):
        trades = [{"ts": 1, "count": 100, "yes_price": 0.5, "taker_side": "yes"}] * 3 + \
                 [{"ts": 2, "count": 5, "yes_price": 0.51, "taker_side": "no"}]
        f = flow_features(trades)
        self.assertGreater(f["imbalance"], 0.9)


class TestEndToEnd(unittest.TestCase):
    def test_dashboard_settled_win_percentage_excludes_open_and_sold(self):
        with tempfile.TemporaryDirectory() as d:
            s = Settings()
            s.db_path = os.path.join(d, "t.db")
            store = Store(s.db_path)
            rows = [
                (1, "paper", "T1", "E", "KXNBAGAME", "yes", 0.5, 1, 0.5, 0.01, "graded", "yes", 0.49),
                (2, "paper", "T2", "E", "KXNBAGAME", "no", 0.5, 1, 0.5, 0.01, "graded", "yes", -0.51),
                (3, "paper", "T3", "E", "KXNBAGAME", "yes", 0.5, 1, 0.5, 0.01, "graded", "sold", 0.10),
                (4, "paper", "T4", "E", "KXNBAGAME", "yes", 0.5, 1, 0.5, 0.01, "open", None, None),
            ]
            for row in rows:
                ts, mode, ticker, event, series, side, price, contracts, stake, fee, status, result, pnl = row
                store.x("INSERT INTO bets(ts,mode,ticker,event_ticker,series,side,price,contracts,stake,fee,status,result,pnl,graded_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, mode, ticker, event, series, side, price, contracts, stake, fee, status, result, pnl, int(time.time()) if status == "graded" else None))
            store.x("UPDATE bets SET exit_ts=?, exit_price=? WHERE ticker='T3'",
                    (int(time.time()) - 10, 0.60))
            page = dashboard.build(store, s, live_path=os.path.join(d, "missing.json"), briefing_path=os.path.join(d, "missing.txt"))
            self.assertIn("Overall · settled · won · win %", page)
            self.assertIn("Today · settled · won · win %", page)
            self.assertIn("2 · 1 · 50.0%", page)
            self.assertIn("All completed bets (3)", page)
            self.assertIn("SOLD EARLY", page)
            self.assertIn("WON", page)
            self.assertIn("LOST", page)
            self.assertIn("exit $0.60", page)
            self.assertIn("T1", page)
            csv_text = dashboard.export_csv(store)
            self.assertIn("entry_time_utc,closed_time_utc", csv_text)
            self.assertIn("exit_ts", csv_text.splitlines()[0])
            self.assertIn("exit_price", csv_text.splitlines()[0])

    def test_dashboard_win_rate_is_scoped_to_selected_mode(self):
        with tempfile.TemporaryDirectory() as d:
            s = Settings()
            s.mode = "paper"
            s.db_path = os.path.join(d, "t.db")
            store = Store(s.db_path)
            now = int(time.time())
            for mode, ticker, result, side in (
                ("paper", "PAPER-WIN", "yes", "yes"),
                ("live", "LIVE-LOSS", "no", "yes"),
            ):
                store.x("""INSERT INTO bets(ts,mode,ticker,event_ticker,series,side,price,contracts,
                            stake,fee,status,result,pnl,graded_ts)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (now, mode, ticker, "E", "KXNBAGAME", side, 0.5, 1, 0.5,
                         0.01, "graded", result, 0.49 if result == side else -0.51, now))
            page = dashboard.build(store, s, live_path=os.path.join(d, "missing.json"),
                                   briefing_path=os.path.join(d, "missing.txt"))
            self.assertIn("Overall · settled · won · win %</div><div class=\"v\">1 · 1 · 100.0%", page)
            self.assertIn("Today · settled · won · win %</div><div class=\"v\">1 · 1 · 100.0%", page)

    def test_dashboard_daily_win_rate_uses_central_time(self):
        with tempfile.TemporaryDirectory() as d:
            s = Settings()
            s.db_path = os.path.join(d, "t.db")
            store = Store(s.db_path)
            # At 04:30 UTC it is still 23:30 the prior day in Central time.
            now_ts = datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc).timestamp()
            settled_ts = datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc).timestamp()
            store.x("""INSERT INTO bets(ts,mode,ticker,event_ticker,series,side,price,
                        contracts,stake,fee,status,result,pnl,graded_ts)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (settled_ts - 3600, "paper", "TZ1", "E", "KXNBAGAME", "yes",
                     0.5, 1, 0.5, 0.01, "graded", "yes", 0.49, int(settled_ts)))
            with patch.dict(os.environ, {"BOT_TIMEZONE": "America/Chicago"}):
                with patch("bot.dashboard.time.time", return_value=now_ts):
                    page = dashboard.build(store, s,
                        live_path=os.path.join(d, "missing.json"),
                        briefing_path=os.path.join(d, "missing.txt"))
            self.assertIn("Today · settled · won · win %</div><div class=\"v\">1 · 1 · 100.0%", page)

    def test_live_partial_fill_records_only_filled_contract_fees(self):
        with tempfile.TemporaryDirectory() as d:
            s = Settings()
            s.mode = "demo"
            s.db_path = os.path.join(d, "bot.sqlite")
            store = Store(s.db_path)
            start = datetime.now(timezone.utc) + timedelta(hours=5)
            engine = Engine(s, store, data_client=FakeKalshi(start),
                            odds_client=FakeOdds(start), schedule_fetch=lambda sp: [])

            class FilledTrade:
                def place_limit_buy(self, ticker, side, contracts, limit):
                    return {"order_id": "ORDER-1", "fill_count": 1,
                            "taker_fill_count": 1, "maker_fill_count": 0,
                            "taker_fill_cost": 50, "maker_fill_cost": 0}

            engine.trade = FilledTrade()
            plan = {"side": "yes", "limit": 0.50, "price": 0.50, "contracts": 10,
                    "stake": 5.00, "fee": risk.taker_fee(10, 0.50, s.taker_fee_rate),
                    "edge": 0.10}
            engine.place({"ticker": "T1", "event_ticker": "E1", "series": "KXNBAGAME",
                          "team": "Boston"}, plan, [], 0.60, 0.90,
                         summary={"bets": 0, "skipped": {}, "alerts": []}, now=int(time.time()))
            saved = store.q("SELECT * FROM bets")[0]
            self.assertEqual(saved["contracts"], 1)
            self.assertEqual(saved["stake"], 0.50)
            self.assertEqual(saved["fee"], risk.taker_fee(1, 0.50, s.taker_fee_rate))

    def test_cycle_bets_grades_and_learns(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["BOT_DB"] = os.path.join(d, "bot.sqlite")
            s = Settings()
            store = Store(s.db_path)
            start = datetime.now(timezone.utc) + timedelta(hours=5)
            kalshi, odds = FakeKalshi(start), FakeOdds(start)

            summary = Engine(s, store, data_client=kalshi, odds_client=odds, schedule_fetch=lambda sp: []).run()
            self.assertEqual(summary["matched"], 1)
            bets = store.open_bets()
            self.assertEqual(len(bets), 1, summary)
            self.assertEqual(bets[0]["side"], "yes")
            self.assertIn("BOS", bets[0]["ticker"])
            self.assertLessEqual(bets[0]["stake"], s.max_per_bet)

            # second cycle must not double up on the same market
            Engine(s, store, data_client=kalshi, odds_client=odds, schedule_fetch=lambda sp: []).run()
            self.assertEqual(len(store.open_bets()), 1)

            # game ends, Boston wins
            for t in ("KXNBAGAME-26OCT20BOSNYK-BOS", "KXNBAGAME-26OCT20BOSNYK-NYK"):
                store.set_kv(f"start_{t}", int(time.time()) - 4 * 3600)
                store.x("UPDATE signal_readings SET ts=ts-5*3600 WHERE ticker=?", (t,))
                store.x("UPDATE market_snapshots SET ts=ts-5*3600 WHERE ticker=?", (t,))
            kalshi.result = "yes"
            out = grade(store, kalshi)
            self.assertEqual(out["bets"], 1)
            self.assertGreaterEqual(out["markets"], 1)
            b = store.q("SELECT * FROM bets")[0]
            self.assertGreater(b["pnl"], 0)
            # weights change, and old values stay in history (nothing deleted)
            self.assertTrue(store.q("SELECT * FROM weight_history"))
            self.assertIn("Signal weights", build(store))
            del os.environ["BOT_DB"]



class FakeCryptoKalshi(FakeKalshi):
    """BTC is $300 above the price to beat with 6 min left, but Kalshi still
    prices UP at 0.60 -> the model should see a big edge and buy YES."""
    def __init__(self):
        super().__init__(datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        self.close = now + timedelta(minutes=6)
        self.event_ticker = "KXBTC15M-" + now.strftime("%y%b%d%H%M").upper()

    def sports_events(self, series):
        if series != "KXBTC15M":
            return []
        return [{"event_ticker": self.event_ticker, "series_ticker": series,
                 "markets": [{"ticker": self.event_ticker + "-30", "status": "active",
                              "floor_strike": 100000.0, "yes_bid": 59, "yes_ask": 60,
                              "close_time": iso(self.close)}]}]

    def orderbook(self, ticker, depth=20):
        return {"yes": [(0.59, 300)], "no": [(0.40, 300)]}


def fake_candles(pair):
    now = int(time.time())
    out, price = [], 100000.0
    for i in range(120):                    # gentle climb, small noise
        price += 2.5 + (3 if i % 2 else -3)
        out.append({"ts": now - (120 - i) * 60, "open": price - 1, "high": price + 2,
                    "low": price - 2, "close": price, "volume": 1.0})
    return out


def fake_spot(pair):
    return fake_candles(pair)[-1]["close"], time.time()


class TestCrypto(unittest.TestCase):
    def test_probability_math(self):
        from bot.signals.crypto import finish_above
        self.assertAlmostEqual(finish_above(100, 100, 0.001, 10), 0.5)
        self.assertGreater(finish_above(101, 100, 0.001, 5), 0.9)
        self.assertEqual(finish_above(99, 100, 0.001, 0), 0.0)

    def test_stale_and_future_sportsbook_rows_are_ignored(self):
        rows = [
            {"ts": 940, "fair_prob": 0.7},
            {"ts": 995, "fair_prob": 0.6},
            {"ts": 1005, "fair_prob": 0.9},
            {"ts": 800, "fair_prob": 0.99},
        ]
        self.assertEqual(_fresh_odds_history(rows, now=1000, max_age_seconds=100),
                         rows[:2])

    def test_bets_lagging_kalshi_price(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["BOT_DB"] = os.path.join(d, "bot.sqlite")
            s = Settings()
            store = Store(s.db_path)
            k = FakeCryptoKalshi()
            out = Engine(s, store, data_client=k, odds_client=FakeOdds(k.start),
                         price_feed=fake_candles, spot_feed=fake_spot).run()
            bets = store.open_bets()
            self.assertEqual(len(bets), 1, out)
            self.assertEqual(bets[0]["side"], "yes")
            self.assertLessEqual(bets[0]["stake"], s.crypto_max_per_bet)
            # window too close to the bell: research only, no bet
            k.close = datetime.now(timezone.utc) + timedelta(seconds=60)
            store.x("DELETE FROM bets")
            Engine(s, store, data_client=k, odds_client=FakeOdds(k.start),
                   price_feed=fake_candles, spot_feed=fake_spot).run()
            self.assertEqual(len(store.open_bets()), 0)
            del os.environ["BOT_DB"]


class TestSchedule(unittest.TestCase):
    def test_crypto_only_run_skips_sports(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["BOT_DB"] = os.path.join(d, "bot.sqlite")
            s = Settings()
            store = Store(s.db_path)
            k = FakeCryptoKalshi()
            out = Engine(s, store, data_client=k, odds_client=FakeOdds(k.start),
                         price_feed=fake_candles, schedule_fetch=lambda sp: [],
                         spot_feed=fake_spot).run(sports=False)
            self.assertEqual(out["matched"], 0)          # no sports work done
            self.assertEqual(len(store.open_bets()), 1)  # crypto still traded
            del os.environ["BOT_DB"]


class TestWatchlist(unittest.TestCase):
    def test_spot_check_only_watched_games(self):
        from bot.watchlist import Watchlist
        with tempfile.TemporaryDirectory() as d:
            os.environ["BOT_DB"] = os.path.join(d, "bot.sqlite")
            s = Settings()
            store = Store(s.db_path)
            start = datetime.now(timezone.utc) + timedelta(hours=5)
            k, o = FakeKalshi(start), FakeOdds(start)
            out = Engine(s, store, data_client=k, odds_client=o, schedule_fetch=lambda sp: []).run(
                watch=Watchlist(teams=["Lakers"]))
            self.assertEqual(out["markets"], 0)          # not watched: skipped
            out = Engine(s, store, data_client=k, odds_client=o, schedule_fetch=lambda sp: []).run(
                watch=Watchlist(teams=["Boston"]))
            self.assertEqual(out["bets"], 1)
            self.assertTrue(out["alerts"])
            del os.environ["BOT_DB"]


class FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class TestAdvanced(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.d.cleanup()

    def test_arbitrage_locks_profit(self):
        from bot.guard import arb_plan
        # YES Boston 0.45 (NO bid 0.55) + YES NY 0.48 (NO bid 0.52) = 0.93 < $1
        a = {"yes": [(0.44, 100)], "no": [(0.55, 100)]}
        b = {"yes": [(0.47, 100)], "no": [(0.52, 100)]}
        plan = arb_plan("KXNBAGAME", a, b, 0.07, 100)
        self.assertIsNotNone(plan)
        self.assertGreater(plan["locked_profit"], 0)
        # NFL games can tie, so both legs could lose: never "arb" them
        self.assertIsNone(arb_plan("KXNFLGAME", a, b, 0.07, 100))
        # 0.50 + 0.51 costs more than $1: no arb
        c = {"yes": [], "no": [(0.50, 100)]}
        d = {"yes": [], "no": [(0.49, 100)]}
        self.assertIsNone(arb_plan("KXNBAGAME", c, d, 0.07, 100))

    def test_drawdown_breaker(self):
        from bot import guard
        self.assertTrue(guard.status(self.store, 1000)[0])
        self.store.x("""INSERT INTO bets(ts, mode, ticker, event_ticker, series, side, price,
                        contracts, stake, fee, status, pnl, graded_ts)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (0, "paper", "T", "E", "KXNBAGAME", "yes", 0.5, 400, 200, 3,
                      "graded", -200, int(time.time()) - 2 * 86400))   # 2 days ago
        ok, why = guard.status(self.store, 1000)
        self.assertFalse(ok)
        self.assertIn("drawdown", why)

    def test_schedule_back_to_back(self):
        from bot.signals.schedule import schedule_spot
        games = [{"date": "2026-10-19T23:00Z", "home": "Boston Celtics", "away": "Miami Heat"},
                 {"date": "2026-10-17T23:00Z", "home": "New York Knicks", "away": "Chicago Bulls"}]
        r = schedule_spot("Boston Celtics", "New York Knicks", False, "2026-10-20T23:30:00Z",
                          games, 0.55, "basketball_nba")
        self.assertLess(r.prob, 0.55)     # Boston on a back-to-back vs rested NY

    def test_llm_panel_median_and_budget(self):
        from bot.llm import LLM
        from bot.signals.intel import ai_news
        replies = iter(['{"adjustment": 0.06, "confidence": 0.8, "reason": "star out"}',
                        '{"adjustment": 0.02, "confidence": 0.6, "reason": "minor"}'])
        llm = LLM("key", self.store, models=["m1", "m2", "m3"], max_per_run=2,
                  daily_limit=10, post=lambda *a, **k: FakeResp(next(replies)))
        heads = [{"headline": "Star ruled out", "description": "ankle"}]
        r = ai_news(llm, "Boston", "New York", 0.55, 0.56, heads)
        self.assertAlmostEqual(r.detail["adjustment"], 0.04)   # median of 0.06, 0.02
        self.assertEqual(r.detail["panel"], ["m1", "m2"])
        self.assertLess(r.confidence, 0.6 * 0.7)                # disagreement penalty
        self.assertEqual(llm.budget_left(), 0)                  # per-run cap respected
        self.assertIsNone(llm.ask("anything")[0])

    def test_llm_falls_back_when_a_model_fails(self):
        from bot.llm import LLM
        calls = []
        def post(url, json=None, **k):
            calls.append(json["model"])
            if json["model"] == "dead":
                raise RuntimeError("model removed")
            return FakeResp("ok")
        llm = LLM("key", self.store, models=["dead", "alive"], post=post)
        text, used = llm.ask("hi")
        self.assertEqual((text, used), ("ok", "alive"))

    def test_briefing_written(self):
        from bot import briefing
        from bot.llm import LLM
        self.store.x("""INSERT INTO bets(ts, mode, ticker, event_ticker, series, side, price,
                        contracts, stake, fee, model_prob, status, result, pnl, clv, graded_ts)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (0, "paper", "T", "E", "KXNBAGAME", "yes", 0.5, 10, 5, 0.2, 0.58,
                      "graded", "yes", 4.8, 0.02, int(time.time())))
        llm = LLM("key", self.store, models=["m"], post=lambda *a, **k: FakeResp("Too early to tell."))
        path = os.path.join(self.d.name, "BRIEFING.txt")
        self.assertEqual(briefing.write(self.store, llm, path), "Too early to tell.")
        self.assertTrue(self.store.q("SELECT * FROM lessons"))
        # quota nearly used up: briefing steps aside for trading decisions
        llm2 = LLM("key", self.store, models=["m"], daily_limit=12,
                   post=lambda *a, **k: FakeResp("x"))
        self.store.set_kv(llm2._day_key(), 5)
        self.assertIsNone(briefing.write(self.store, llm2, path))


class TestBacktestAndDomains(unittest.TestCase):
    def test_calibration_table_excludes_outcomes_settled_after_as_of_time(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "calibration-time.sqlite"))
            for i, settled_ts in enumerate((1000, 2000)):
                ticker = f"ASOF-{i}"
                store.add_decision_audit({
                    "ts": 100 + i, "ticker": ticker, "event_ticker": f"E{i}",
                    "series": "KXNBAGAME", "decision": "rejected", "reason": "test",
                    "side": "yes", "market_price": 0.5, "model_prob": 0.7,
                    "side_prob": 0.7, "edge": 0.2, "confidence": 0.8,
                })
                store.grade_decision_audit(ticker, 1, settled_ts=settled_ts, cutoff_ts=100 + i)
            table = brain.calibration_table(store, domain="sports", as_of_ts=1500)
            self.assertEqual(table[7]["n"], 1)
            self.assertEqual(table[7]["actual"], 1.0)

    def test_backtest_weights_are_reconstructed_as_of_decision_time(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "weights.sqlite"))
            store.x("INSERT INTO weight_history VALUES(?,?,?,?)",
                    (100 * 1_000_000_000, "book_consensus", 2.0, "past"))
            store.x("INSERT INTO weight_history VALUES(?,?,?,?)",
                    (200 * 1_000_000_000, "book_consensus", 9.0, "future"))
            weights = backtest._weights_as_of(store, "sports", 150)
            self.assertEqual(weights["book_consensus"], 2.0)

    def test_backtest_and_separate_learning(self):
        from bot import backtest
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "t.db"))
            start = int(time.time()) - 7200
            # 30 crypto windows where the model was right and Kalshi lagged
            for i in range(30):
                t = f"KXBTC15M-X{i}"
                y = i % 3 != 0
                store.set_kv(f"start_{t}", start)
                store.add_signal(start - 300, t, "kalshi_market", 0.60, 0.95,
                                 {"mid": 0.60, "spread": 0.01})
                store.add_signal(start - 300, t, "crypto_model", 0.85 if y else 0.30, 0.75, {})
                store.x("INSERT INTO graded_markets VALUES(?,?,?,?)", (t, start, int(y), ""))
            r = backtest.run(store)["by_domain"]["crypto"]
            self.assertLess(r["model_brier"], r["market_brier"])
            self.assertGreater(r["thresholds"][0.05]["bets"], 0)
            self.assertIn("BACKTEST", backtest.text(backtest.run(store)))
            # learning on crypto must not touch sports weights
            graded = [{"kalshi_market": 0.6, "crypto_model": 0.85, "_outcome": 1}] * 20
            brain.learn(store, graded, "crypto")
            self.assertGreater(brain.get_weights(store, "crypto")["crypto_model"],
                               brain.DEFAULT_WEIGHTS["crypto_model"])
            self.assertEqual(brain.get_weights(store, "sports")["crypto_model"],
                             brain.DEFAULT_WEIGHTS["crypto_model"])


class TestSigning(unittest.TestCase):
    def test_rsa_pss_signature_verifies(self):
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from bot.kalshi_client import KalshiClient
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        c = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "kid", pem)
        h = c._headers("GET", "/portfolio/balance?x=1")
        msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/v2/portfolio/balance").encode()
        key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                            salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())


if __name__ == "__main__":
    unittest.main()
