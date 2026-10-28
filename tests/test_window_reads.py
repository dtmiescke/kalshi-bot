"""Token, liquidity and volume inputs, the opening read of every 15-minute chart,
and the verdict once it settles."""
import json
import os
import tempfile
import time
import unittest

import numpy as np

from bot import brain, features, ml, token_data, window_review
from bot.signals.base import Reading
from bot.storage import Store

PAPRIKA = {"quotes": {"USD": {"market_cap": 1.6e12, "volume_24h": 1.8e10, "volume_24h_change_24h": -4.0,
                              "percent_change_15m": -0.08, "percent_change_30m": -0.12, "percent_change_1h": -0.18,
                              "percent_change_6h": -0.39, "percent_change_24h": 0.5, "percent_change_7d": 3.0}},
           "rank": 1}
COINBASE = {"open": "79900", "high": "81700", "low": "79700", "last": "80700", "volume": "4000", "volume_30day": "120000"}
STABLES = {"peggedAssets": [
    {"symbol": "USDT", "circulating": {"peggedUSD": 101.0}, "circulatingPrevDay": {"peggedUSD": 100.0}},
    {"symbol": "USDC", "circulating": {"peggedUSD": 51.0}, "circulatingPrevDay": {"peggedUSD": 50.0}},
    {"symbol": "DAI", "circulating": {"peggedUSD": 999.0}, "circulatingPrevDay": {"peggedUSD": 1.0}}]}


class Resp:
    def __init__(self, data, ok=True):
        self.data, self.ok = data, ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("down")

    def json(self):
        return self.data


def fake_get(down=()):
    def get(url, params=None, timeout=10, headers=None):
        for key, data in (("coinpaprika", PAPRIKA), ("coinbase.com", COINBASE), ("llama", STABLES)):
            if key in url:
                return Resp(data, ok=key not in down)
        return Resp({}, ok=False)
    return get


def candles(n=80, vol=1.0):
    return [{"ts": 1_700_000_000 + 60 * i, "open": 100.0, "high": 100.5, "low": 99.5,
             "close": 100.2 if i % 3 else 99.8, "volume": vol * (3 if i >= n - 5 else 1)} for i in range(n)]


class Book:
    yes = {0.55: 300.0, 0.52: 200.0, 0.30: 900.0}
    no = {0.43: 100.0, 0.40: 100.0}


class TestTokenData(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def test_snapshot_reads_each_free_source_and_caches(self):
        s = token_data.coin_snapshot(self.st, "BTC", get=fake_get())
        self.assertEqual(s["paprika"]["rank"], 1)
        self.assertAlmostEqual(s["day"]["avg_daily_volume"], 4000.0)
        self.assertAlmostEqual(s["stable_chg"], 152 / 150 - 1)                # only USDT + USDC
        calls = []
        token_data.coin_snapshot(self.st, "BTC", get=lambda *a, **k: calls.append(1) or Resp({}, ok=False))
        self.assertEqual(calls, [])                                          # served from cache

    def test_a_source_that_is_down_is_just_missing(self):
        s = token_data.coin_snapshot(self.st, "BTC", get=fake_get(down=("coinpaprika", "llama")))
        self.assertIsNone(s["paprika"])
        self.assertIsNone(s["stable_chg"])
        self.assertIsNotNone(s["day"])
        f = token_data.token_features({}, [], snapshot=s)
        self.assertNotIn("tk_has", f)                                         # no token data: not claimed

    def test_kalshi_liquidity_comes_from_the_live_book(self):
        k = token_data.kalshi_liquidity({"volume_fp": "669089.26", "open_interest_fp": "263864.94"}, Book)
        self.assertEqual(k["depth_5c_yes"], 500.0)                            # 0.55 and 0.52, not 0.30
        self.assertEqual(k["top_no_bid_size"], 100.0)
        self.assertEqual(token_data.kalshi_liquidity(None, None), {"volume": None, "volume_24h": None,
                         "open_interest": None, "bid_size": None, "ask_size": None})

    def test_features_are_scaled_ratios_and_known_names(self):
        snap = token_data.coin_snapshot(self.st, "BTC", get=fake_get())
        ctx = {"orderbook": {"mid": 100.0, "bid_depth": 50.0, "ask_depth": 50.0, "spread_bps": 2.0,
                             "depth_imbalance": 0.1}, "funding": 0.0001, "put_call_oi": 0.7}
        f = token_data.token_features(ctx, candles(), token_data.kalshi_liquidity({"volume_fp": "1000"}, Book), snap)
        self.assertEqual(f["tk_has"], 1.0)
        self.assertAlmostEqual(f["tk_turnover"], 1.8e10 / 1.6e12)
        self.assertAlmostEqual(f["tk_chg_24h"], 0.005)                        # percent -> fraction
        self.assertAlmostEqual(f["tk_range_pos_24h"], (80700 - 79700) / 2000)
        self.assertAlmostEqual(f["tk_relvol_30d"], 1.0)
        self.assertAlmostEqual(f["tk_ex_depth_usd_log"], np.log10(10000))
        self.assertGreater(f["tk_vol_15m_vs_60m"], 1.0)                       # the last five minutes were busy
        self.assertEqual(f["tk_funding"], 0.0001)
        self.assertTrue(set(f) <= set(token_data.FEATURE_NAMES))

    def test_model_inputs_include_them_and_old_rows_still_load(self):
        self.assertTrue(set(token_data.FEATURE_NAMES) <= set(features.names()))
        old = {n: 0.1 for n in features.names() if not n.startswith("tk_")}
        new = dict(old, tk_has=1.0, tk_turnover=0.011)
        for i, f in enumerate((old, new)):
            ml.add_row(self.st, f"KXBTC15M-{i}", 100 + i, "BTC", f, 1, "live")
        X, y, groups, ts, cols = ml.load_dataset(self.st)
        self.assertEqual(X.shape[0], 2)
        ti = cols.index("tk_has")
        self.assertEqual(list(X[:, ti]), [0.0, 1.0])                          # old rows read as "no token data"


class TestWindowReview(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.now = int(time.time())
        self.terms = {"strike": 80500.0, "close_ts": self.now + 14 * 60, "avg_seconds": 60}
        self.info = {"series": "KXBTC15M"}
        self.readings = [Reading("kalshi_market", 0.52, 0.9, {}), Reading("crypto_model", 0.62, 0.75, {}),
                         Reading("crypto_momentum", 0.40, 0.3, {})]
        self.ctx = {"kalshi_yes_bid": 0.51, "kalshi_yes_ask": 0.53, "spot": {"composite": 80620.0, "source_count": 3},
                    "features": {"adx_1m": 31.0, "ema_9_21": 0.4, "rsi_15m": 64.0, "tk_vol_15m_vs_60m": 1.8,
                                 "vol_ratio_15_60": 1.4, "tk_k_depth_5c_log": 4.2, "tk_has": 1.0},
                    "chart_setups": {"setups": [{"name": "breakout"}]}, "market_context": {"funding": 0.0001}}

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def record(self):
        return window_review.record_open(self.st, "KXBTC15M-T1", self.info, self.terms, self.now,
                                         self.readings, self.ctx, brain.DEFAULT_WEIGHTS)

    def test_the_opening_read_is_written_once_with_everything_in_it(self):
        row = self.record()
        self.assertIsNotNone(row)
        self.assertIsNone(self.record())                                      # one read per window
        snap = json.loads(row["snapshot"])
        self.assertEqual(snap["token"]["tk_has"], 1.0)
        self.assertEqual(snap["setups"], ["breakout"])
        self.assertIn("crypto_model", json.loads(row["signals"]))
        self.assertEqual(row["coin"], "BTC")
        self.assertEqual(row["open_ts"], self.terms["close_ts"] - 900)
        self.assertIn("Trending up (ADX 31)", row["summary"])
        self.assertIn("volume surging", row["summary"])
        self.assertIn("Bot 57% vs Kalshi 52%", row["summary"])
        self.assertIn("above the target", row["summary"])

    def test_settling_adds_what_really_happened(self):
        self.record()
        got = window_review.settle(self.st, "KXBTC15M-T1", 1, self.now + 900)
        self.assertLess(got["bot_brier"], got["kalshi_brier"])                # bot leaned YES, YES won
        r = self.st.q("SELECT * FROM window_log")[0]
        self.assertEqual(r["best_signal"], "crypto_model")
        self.assertEqual(r["worst_signal"], "crypto_momentum")
        self.assertIn("YES won", r["note"])
        self.assertIsNone(window_review.settle(self.st, "KXBTC15M-T1", 1))   # a window settles once
        with self.assertRaises(ValueError):
            window_review.settle(self.st, "KXBTC15M-T1", 2)

    def test_report_scores_the_bot_against_kalshi_over_every_window(self):
        self.record()
        window_review.settle(self.st, "KXBTC15M-T1", 1)
        rep = window_review.report(self.st)
        self.assertEqual(rep["stats"]["windows"], 1)
        self.assertGreater(rep["stats"]["skill"], 0)
        self.assertEqual(rep["stats"]["hit_rate"], 1.0)
        self.assertEqual(rep["recent"][0]["coin"], "BTC")
        self.assertIn("BTC", rep["stats"]["by_coin"])

    def test_no_read_without_a_kalshi_price(self):
        self.ctx.pop("kalshi_yes_bid")
        self.assertIsNone(self.record())


class TestSimIgnoresEarlyRows(unittest.TestCase):
    def test_rows_from_before_the_bot_trades_are_not_simulated(self):
        cols = ["kalshi_logit", "kalshi_spread", "min_left"]
        X = np.array([[0.0, 0.0, 14.5], [0.0, 0.0, 10.0]])
        out = ml.sim_trades(np.array([0.9, 0.9]), X, cols, np.array([1, 1]), np.array(["w", "w"]), thresholds=(0.03,))
        self.assertEqual(out[0.03]["trades"], 1)                              # the 14.5-minute row was skipped


if __name__ == "__main__":
    unittest.main()
