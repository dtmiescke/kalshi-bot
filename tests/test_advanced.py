"""Offline tests for the trained model, indicators, vol forecaster,
execution (maker / exits / caps / fixed bet) and ladder arbitrage."""
import math
import os
import random
import tempfile
import time
import unittest

from bot import features, guard, ml, risk, vol as volmod, marketdata
from bot.config import Settings
from bot.storage import Store


def walk(n, start=80000.0, sd=0.0006, seed=1, t0=1_750_000_000):
    rnd = random.Random(seed)
    p, out = start, []
    for i in range(n):
        o = p
        p *= 1 + rnd.gauss(0, sd)
        out.append({"ts": t0 + 60 * i, "open": o, "high": max(o, p), "low": min(o, p),
                    "close": p, "volume": rnd.random() * 3})
    return out


class TestFeatures(unittest.TestCase):
    def test_build_is_scale_free_and_complete(self):
        c = walk(400)
        f1 = features.build(c, c[-1]["close"] * 1.001, 400, 0.4, 0.42)
        scaled = [{k: (v * 0.5 if k in ("open", "high", "low", "close") else v)
                   for k, v in x.items()} for x in c]
        f2 = features.build(scaled, c[-1]["close"] * 0.5005, 400, 0.4, 0.42)
        self.assertEqual(sorted(f1), features.names())
        for k in ("dist_sigma", "rsi_1m", "macd_hist", "bb_pctb", "ret_5m"):
            self.assertAlmostEqual(f1[k], f2[k], places=4)   # same meaning at any price
        self.assertTrue(all(math.isfinite(v) for v in f1.values()))

    def test_indicators(self):
        up = [{"ts": i * 60, "open": 100 + i, "high": 101 + i, "low": 99 + i,
               "close": 100 + i, "volume": 1} for i in range(100)]
        import numpy as np
        self.assertGreater(features.rsi(np.array([c["close"] for c in up])), 90)
        self.assertGreater(features.adx(*[np.array([c[k] for c in up]) for k in ("high", "low", "close")]), 25)


class TestVol(unittest.TestCase):
    def test_forecast_blend_season_and_events(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            closes = [c["close"] for c in walk(200)]
            base, parts = volmod.forecast(st, "BTC", closes, dvol_pct=50, now=1_750_000_000)
            self.assertTrue(0.0002 < base < 0.002)
            ev = [{"ts": 1_750_000_000 + 600, "impact": "High"}]
            hot, _ = volmod.forecast(st, "BTC", closes, 50, 1_750_000_000, ev)
            self.assertAlmostEqual(hot / base, 1.8, places=3)
            # learning moves weight toward the view that matched reality
            samples = [({"ewma": 0.0006, "long": 0.0006, "implied": 0.002}, 0.0006)] * 40
            b = volmod.learn_blend(st, "BTC", samples)
            self.assertLess(b["implied"], volmod.DEFAULT_BLEND["implied"])


class TestModel(unittest.TestCase):
    def test_shared_roi_accounting_includes_fees(self):
        from bot.metrics import binary_contract_roi, net_roi
        self.assertAlmostEqual(net_roi(0.50, 1.10), 0.50 / 1.10)
        self.assertIsNone(net_roi(1.0, 0.0))
        pnl, capital, roi = binary_contract_roi([
            {"won": True, "cost": 0.505, "fee": 0.02},
            {"won": False, "cost": 0.40, "fee": 0.02},
        ])
        self.assertAlmostEqual(pnl, (1 - 0.505 - 0.02) + (-0.40 - 0.02))
        self.assertAlmostEqual(capital, 0.505 + 0.02 + 0.40 + 0.02)
        self.assertAlmostEqual(roi, pnl / capital)

    def test_simulated_single_contract_fee_rounds_up_to_cent(self):
        # Live risk.taker_fee rounds each order's fee up to a cent. The
        # simulator must not use fractional cents that inflate backtest ROI.
        self.assertEqual(risk.taker_fee(1, 0.505, 0.07), 0.02)

        import numpy as np
        cols = ["kalshi_logit", "kalshi_spread"]
        X = np.array([[0.0, 0.0]])
        # With a 50% market and 50.5c simulated entry, a 60% model probability
        # has 7.5c gross edge; fee must be the live-compatible 2c.
        out = ml.sim_trades(
            np.array([0.60]), X, cols, np.array([1]), np.array(["window"]),
            fee_rate=0.07, thresholds=(0.03,)
        )
        self.assertEqual(out[0.03]["trades"], 1)
        # ROI uses total capital committed (premium + fee), not premium alone.
        self.assertAlmostEqual(
            out[0.03]["roi"], (1 - 0.505 - 0.02) / (0.505 + 0.02), places=4
        )

    def test_train_promote_predict(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["MODEL_DIR"] = os.path.join(d, "models")
            ml.MODEL_DIR = os.environ["MODEL_DIR"]
            ml._CACHE.clear()
            st = Store(os.path.join(d, "t.db"))
            rnd = random.Random(5)
            c = walk(800 * 15 + 400, seed=9)
            n = 0
            for w in range(400, len(c) - 16, 15):
                strike = c[w]["close"]
                y = int(c[w + 15]["close"] > strike)
                for k in (4, 8):
                    i = w + k
                    f0 = features.build(c[i - 300:i + 1], strike, (15 - k) * 60)
                    mid = min(max(0.5 + 0.6 * (f0["model_prob"] - 0.5) + rnd.gauss(0, .04), .03), .97)
                    f = features.build(c[i - 300:i + 1], strike, (15 - k) * 60, mid - .01, mid + .01)
                    ml.add_row(st, f"KXBTC15M-S{w}", c[i]["ts"], "BTC", f, y, "test")
                n += 1
                if n >= 800:
                    break
            m = ml.train(st, min_windows=300)
            self.assertTrue(m["beats_kalshi"], m)
            self.assertTrue(m["promoted"], m)
            p, meta = ml.predict(f, "BTC")
            self.assertTrue(0 < p < 1)
            self.assertIn("top_features", meta)
            self.assertIn("PROMOTED", ml.text(m))
            del os.environ["MODEL_DIR"]

    def test_too_little_data(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            self.assertEqual(ml.train(st)["status"], "not enough data")


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()

    def tearDown(self):
        self.d.cleanup()

    def test_maker_prices_inside_spread(self):
        book = {"yes": [(0.50, 100)], "no": [(0.45, 100)]}      # YES bid .50, ask .55
        plan, _ = risk.decide(self.s, self.st, {"ticker": "T", "event_ticker": "E"},
                              0.70, 0.9, book, maker=True)
        self.assertEqual((plan["style"], plan["price"]), ("maker", 0.51))
        taker, _ = risk.decide(self.s, self.st, {"ticker": "T", "event_ticker": "E"},
                               0.70, 0.9, book, maker=False)
        self.assertGreater(plan["edge"], taker["edge"])          # cheaper price + lower fee

    def test_fixed_bet_and_window_cap(self):
        self.s.fixed_bet = 5
        book = {"yes": [(0.50, 500)], "no": [(0.45, 500)]}
        plan, _ = risk.decide(self.s, self.st, {"ticker": "T", "event_ticker": "E"},
                              0.75, 0.9, book)
        self.assertLessEqual(plan["stake"], 5)
        self.s.fixed_bet, self.s.crypto_max_per_window = 15, 20
        self.st.add_bet({"ts": 1, "mode": "paper", "ticker": "KXBTC15M-W1-00",
                         "event_ticker": "KXBTC15M-W1", "series": "KXBTC15M", "team": "UP",
                         "side": "yes", "price": .5, "contracts": 39, "stake": 19.5, "fee": .3,
                         "model_prob": .7, "edge": .1, "confidence": .8, "signals": {}})
        plan, why = risk.decide(self.s, self.st, {"ticker": "KXETH15M-W1-00", "event_ticker": "KXETH15M-W1",
                                                  "series": "KXETH15M"}, 0.75, 0.9, book,
                                max_per_bet=10)
        self.assertIsNone(plan)                                  # BTC already used the window
        self.assertIn("caps", why)

    def test_close_bet_records_sale(self):
        bid = self.st.add_bet({"ts": 1, "mode": "paper", "ticker": "T", "event_ticker": "E",
                               "series": "KXBTC15M", "team": "UP", "side": "yes", "price": .4,
                               "contracts": 10, "stake": 4, "fee": .1, "model_prob": .6,
                               "edge": .1, "confidence": .8, "signals": {}})
        self.st.close_bet(bid, 0.7, 10 * 0.3 - 0.2)
        b = self.st.q("SELECT * FROM bets")[0]
        self.assertEqual((b["result"], b["status"]), ("sold", "graded"))
        self.assertAlmostEqual(b["pnl"], 2.8)

    def test_ladder_arbitrage(self):
        ms = [{"ticker": "LOW", "strike": 81900, "close_ts": 1, "rules_key": "k",
               "yes_bid": .40, "yes_ask": .42},
              {"ticker": "HIGH", "strike": 82000, "close_ts": 1, "rules_key": "k",
               "yes_bid": .60, "yes_ask": .62},
              {"ticker": "OTHER", "strike": 82000, "close_ts": 1, "rules_key": "different",
               "yes_bid": .90, "yes_ask": .92}]
        found = guard.ladder_arbs(ms, 0.07)
        self.assertEqual([(f["low"], f["high"]) for f in found], [("LOW", "HIGH")])
        # consistent prices: no arbitrage
        ms[1].update(yes_bid=.30, yes_ask=.32)
        self.assertEqual(guard.ladder_arbs(ms[:2], 0.07), [])


class TestMarketData(unittest.TestCase):
    def test_context_parsing(self):
        class R:
            def __init__(self, d):
                self.d = d

            def raise_for_status(self):
                pass

            def json(self):
                return self.d

        def get(url, params=None, **k):
            if "futures" in url:
                return R({"tickers": [{"symbol": "PF_XBTUSD", "fundingRate": 0.0001,
                                       "openInterest": 1000, "markPrice": 101, "indexPrice": 100}]})
            if "kraken.com/0" in url:
                return R({"result": {"XXBTZUSD": {"c": ["100.0", "1"]}}})
            if "deribit" in url:
                return R({"result": [{"instrument_name": "BTC-X-P", "open_interest": 60},
                                     {"instrument_name": "BTC-X-C", "open_interest": 100}]})
            if "alternative" in url:
                return R({"data": [{"value": "72"}]})
            return R([])
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            c = marketdata.context(st, "BTC", coinbase_price=100.5, get=get)
            self.assertAlmostEqual(c["basis"], 0.01)
            self.assertAlmostEqual(c["put_call_oi"], 0.6)
            self.assertEqual(c["fear_greed"], 72)
            self.assertAlmostEqual(c["coinbase_premium"], 0.005)


if __name__ == "__main__":
    unittest.main()


class TestChampionArtifactSafety(unittest.TestCase):
    def test_champion_metadata_is_embedded_and_atomically_promoted(self):
        from unittest.mock import patch
        import json
        import joblib

        with tempfile.TemporaryDirectory() as d:
            meta = {"feature_version": features.FEATURE_VERSION, "trained": 123}
            bundle = {"model": "candidate", "iso": "calibrator", "cols": ["kalshi_logit"],
                      "meta": meta}
            with patch.object(ml, "MODEL_DIR", d):
                ml._promote_champion(bundle, meta, joblib)
                active = joblib.load(os.path.join(d, "crypto_model.joblib"))
                with open(os.path.join(d, "crypto_model.json"), encoding="utf-8") as f:
                    sidecar = json.load(f)
                self.assertEqual(active["meta"], meta)
                self.assertEqual(sidecar, meta)

    def test_interrupted_sidecar_write_keeps_model_metadata_consistent(self):
        from unittest.mock import patch
        import joblib

        with tempfile.TemporaryDirectory() as d:
            old_meta = {"feature_version": features.FEATURE_VERSION, "trained": 1}
            old_bundle = {"model": "old", "iso": "old-calibrator", "cols": ["kalshi_logit"],
                          "meta": old_meta}
            ml._promote_champion(old_bundle, old_meta, joblib)
            new_meta = {"feature_version": features.FEATURE_VERSION, "trained": 2}
            new_bundle = {"model": "new", "iso": "new-calibrator", "cols": ["kalshi_logit"],
                          "meta": new_meta}
            real_replace = os.replace
            calls = {"n": 0}

            def fail_second_replace(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated interruption before metadata sidecar commit")
                return real_replace(src, dst)

            with patch.object(ml, "MODEL_DIR", d), patch.object(os, "replace", side_effect=fail_second_replace):
                with self.assertRaises(OSError):
                    ml._promote_champion(new_bundle, new_meta, joblib)
                active = joblib.load(os.path.join(d, "crypto_model.joblib"))
                self.assertEqual(active["meta"], new_meta)
                # The sidecar can lag after a crash, but prediction metadata travels
                # inside the atomically committed model artifact and stays consistent.
                self.assertEqual(ml.load_meta(), old_meta)
                self.assertTrue(any(name.endswith(".joblib") and name.startswith("crypto_model_")
                                    for name in os.listdir(d)))
