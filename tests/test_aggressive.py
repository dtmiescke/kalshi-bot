"""Tests for the aggressive upgrades: ladders, learned maker fill rate,
earned sizing, separate data key, and the WebSocket fallback."""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from bot import brain, risk
from bot.config import Settings
from bot.engine import Engine
from bot.storage import Store

# These fixtures use small synthetic stakes; production minimum bets ($25) are
# asserted with a clean environment in test_total_loss_limit.py (CI sets the same).
os.environ.setdefault("MIN_PER_BET", "0")
os.environ.setdefault("CRYPTO_MIN_BET", "0")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def candles(now, price=100300.0, n=180):
    out = []
    for i in range(n):
        p = price + (i % 7) * 4 - 12
        out.append({"ts": int(now) - (n - i) * 60, "open": p - 1, "high": p + 3, "low": p - 3,
                    "close": p, "volume": 1.0 + i % 3})
    return out


class FakeLadderKalshi:
    """Hourly BTC ladder, 60 min left, BTC at ~$100,300. 'Above $100,000' is far
    too cheap at $0.55; 'above $100,200' is a touch cheap; one strike per event."""
    def __init__(self):
        self.close = datetime.now(timezone.utc) + timedelta(minutes=60)

    def sports_events(self, series):
        if series != "KXBTCD":
            return []
        mk = lambda t, k, b, a: {"ticker": t, "status": "active", "floor_strike": k,
                                 "strike_type": "greater", "yes_bid": b, "yes_ask": a,
                                 "close_time": iso(self.close)}
        return [{"event_ticker": "KXBTCD-H1", "markets": [
            mk("KXBTCD-H1-T100000", 100000.0, 54, 55), mk("KXBTCD-H1-T100200", 100200.0, 50, 51)]}]

    def orderbook(self, ticker):
        if ticker.endswith("T100000"):
            return {"yes": [(0.54, 300)], "no": [(0.45, 300)]}
        return {"yes": [(0.50, 300)], "no": [(0.49, 300)]}

    def trades(self, ticker, min_ts=None):
        return []


class TestAggressive(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        os.environ["BOT_DB"] = os.path.join(self.d.name, "b.db")
        self.s = Settings()
        self.st = Store(self.s.db_path)

    def tearDown(self):
        del os.environ["BOT_DB"]
        self.d.cleanup()

    def test_ladders_trade_best_strike_once(self):
        now = time.time()
        e = Engine(self.s, self.st, data_client=FakeLadderKalshi(),
                   price_feed=lambda p: candles(now), spot_feed=lambda p: (100300.0, time.time()),
                   schedule_fetch=lambda sp: [])
        os.environ["CRYPTO_LADDER_SERIES"] = "KXBTCD"
        try:
            e.run_ladders({"bets": 0, "skipped": {}})
        finally:
            del os.environ["CRYPTO_LADDER_SERIES"]
        bets = self.st.open_bets()
        self.assertEqual(len(bets), 1)                       # one strike per event
        self.assertEqual(bets[0]["ticker"], "KXBTCD-H1-T100000")
        self.assertEqual(bets[0]["side"], "yes")
        self.assertGreaterEqual(bets[0]["stake"], 15)       # all crypto trades meet minimum\n        self.assertLessEqual(bets[0]["stake"], 20)            # default ladder cap includes fees
        self.assertEqual(brain.domain_of("KXBTCD-H1"), "crypto")

    def test_earned_size_ramps_with_proof(self):
        mkt = {"ticker": "KXBTC15M-X", "series": "KXBTC15M"}
        self.assertEqual(risk.earned_size(self.st, mkt), 0.25)      # no record: small
        for i in range(200):
            self.st.add_bet({"ts": 1, "mode": "paper", "ticker": f"KXBTC15M-{i}",
                             "event_ticker": f"KXBTC15M-{i}", "series": "KXBTC15M", "team": "UP",
                             "side": "yes", "price": .5, "contracts": 2, "stake": 1, "fee": .02,
                             "model_prob": .6, "edge": .05, "confidence": .8, "signals": {}})
        self.st.x("UPDATE bets SET status='graded', pnl=0.1")
        self.assertEqual(risk.earned_size(self.st, mkt), 1.0)       # 200 profitable bets: full
        self.st.x("UPDATE bets SET pnl=-0.1")
        self.assertEqual(risk.earned_size(self.st, mkt), 0.25)      # losing record: back to floor
        self.assertEqual(risk.earned_size(self.st, {"series": "KXNBAGAME"}), 0.25)

    def test_maker_fill_rate(self):
        for i in range(25):
            self.st.log_maker("T", 1 if i < 3 else 0, "expired", 45)
        rate, n = self.st.maker_fill_rate()
        self.assertEqual(n, 25)
        self.assertLess(rate, 0.25)

    def test_data_key_separate_from_trading_key(self):
        os.environ.update({"KALSHI_API_KEY_ID": "demo-id", "KALSHI_PRIVATE_KEY": "demo-pem"})
        try:
            s = Settings()
            self.assertEqual(s.data_key(), (None, None))             # paper: demo key never used for data
            os.environ.update({"KALSHI_DATA_KEY_ID": "real-id", "KALSHI_DATA_PRIVATE_KEY": "real-pem"})
            self.assertEqual(Settings().data_key(), ("real-id", "real-pem"))
        finally:
            for k in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_DATA_KEY_ID",
                      "KALSHI_DATA_PRIVATE_KEY"):
                os.environ.pop(k, None)

    def test_quiet_book_gets_polled_even_with_websocket(self):
        from bot.live.runner import LiveRunner

        class Rest(FakeLadderKalshi):
            def orderbook(self, t):
                return {"yes": [(0.40, 10)], "no": [(0.55, 10)]}
        r = LiveRunner(self.s, self.st, rest=Rest(), llm=None, seed=False, with_context=False)
        from bot.live.kalshi_book import KalshiBook
        fresh, quiet = KalshiBook("A"), KalshiBook("B")
        fresh.yes, fresh.no, fresh.last_update = {0.3: 1}, {0.6: 1}, time.time()
        quiet.last_update = time.time() - 30                     # websocket went silent
        r.books = {"A": fresh, "B": quiet}
        r.poll_books(False, True)
        self.assertEqual(fresh.yes, {0.3: 1})                    # live book left alone
        self.assertEqual(quiet.yes, {0.40: 10})                  # silent book refreshed


if __name__ == "__main__":
    unittest.main()


class TestOmniRoute(unittest.TestCase):
    def test_omniroute_first_then_openrouter_fallback(self):
        import requests
        from bot.llm import LLM
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            calls = []

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"choices": [{"message": {"content": "OK"}}]}

            def post(url, json=None, **k):
                calls.append((url, json["model"]))
                if "omni" in url:
                    raise requests.ConnectionError("gateway down")
                return R()
            llm = LLM("or-key", st, post=post, endpoints=[
                ("omniroute", "https://omni.example/v1/chat/completions", "k"),
                ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "or-key")],
                models=["omniroute|auto", "omniroute|fast", "meta-llama/x:free"])
            text, used = llm.ask("hi")
            self.assertEqual((text, used), ("OK", "meta-llama/x:free"))
            # gateway down -> its second model is skipped, not retried
            self.assertEqual([c[1] for c in calls], ["auto", "meta-llama/x:free"])


class TestDashboard(unittest.TestCase):
    def test_dashboard_and_csv(self):
        from bot import dashboard
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            s = Settings()
            s.bankroll = 1000.0  # isolated dashboard fixture; production default is $200k
            st.add_bet({"ts": int(time.time()), "mode": "paper", "ticker": "KXBTC15M-X-00",
                        "event_ticker": "KXBTC15M-X", "series": "KXBTC15M", "team": "UP",
                        "side": "yes", "price": .45, "contracts": 10, "stake": 4.5, "fee": .17,
                        "model_prob": .6, "edge": .1, "confidence": .8, "signals": {}})
            out = dashboard.write(st, s, os.path.join(d, "site"))
            page = open(os.path.join(out, "index.html")).read()
            self.assertIn("BTC 15m", page)                    # short, phone-friendly name
            self.assertIn("prefers-color-scheme:dark", page)
            self.assertIn("never", page)                      # no quote yet: flagged
            csv_text = open(os.path.join(out, "bets.csv")).read()
            self.assertIn("KXBTC15M-X-00", csv_text)
            self.assertIn("$995.33", page)                    # cash = 1000 - 4.50 - 0.17
