"""Hyperliquid perps: one cached keyless request feeds two small, separately
learned crypto readings (funding crowding and perp premium). Fails soft."""
import os
import tempfile
import unittest

from bot import brain, crypto_brain, marketdata
from bot.storage import Store


class FakeResp:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)

    def json(self):
        return self.body


BODY = [{"universe": [{"name": "BTC"}, {"name": "DOGE"}, {"name": "BNB"}]},
        [{"funding": "0.0000125", "premium": "-0.0002", "openInterest": "43574.5"},
         {"funding": "0.001", "premium": "0", "openInterest": "1"},
         {"funding": "0.0003", "premium": "0.0005", "openInterest": "59872.57"}]]


class TestHyperliquid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.sqlite"))
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, json=None, **kw):
        self.calls.append((url, json))
        return FakeResp(BODY)

    def test_parses_and_caches_one_request_for_all_coins(self):
        btc = marketdata.hyperliquid(self.store, "BTC", post=self.post)
        bnb = marketdata.hyperliquid(self.store, "BNB", post=self.post)
        self.assertEqual({k: btc[k] for k in ("funding", "premium", "open_interest")},
                         {"funding": 0.0000125, "premium": -0.0002, "open_interest": 43574.5})
        self.assertAlmostEqual(bnb["funding"], 0.0003)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], (marketdata.HYPERLIQUID_INFO, {"type": "metaAndAssetCtxs"}))
        self.assertEqual(marketdata.hyperliquid(self.store, "DOGE", post=self.post), {})

    def test_outage_returns_empty_and_is_not_cached(self):
        def down(*a, **k):
            raise ConnectionError("down")
        self.assertEqual(marketdata.hyperliquid(self.store, "BTC", post=down), {})
        self.assertEqual(marketdata.hyperliquid(self.store, "BTC", post=lambda *a, **k: FakeResp({}, 500)), {})
        self.assertIsNone(self.store.get_kv("hl_ctx"))
        self.assertTrue(marketdata.hyperliquid(self.store, "BTC", post=self.post))


class TestPerpReadings(unittest.TestCase):
    def by_name(self, ctx, p=0.5):
        return {r.name: r for r in crypto_brain.perp_readings(ctx, p)}

    def test_neutral_funding_and_premium_leave_price_alone(self):
        r = self.by_name({"hl_funding": crypto_brain.HL_BASE_FUNDING, "hl_premium": 0.0})
        self.assertAlmostEqual(r["crypto_perp_funding"].prob, 0.5)
        self.assertAlmostEqual(r["crypto_perp_premium"].prob, 0.5)

    def test_crowded_longs_lean_down_and_rich_perps_lean_up(self):
        r = self.by_name({"hl_funding": 0.0005, "hl_premium": 0.002})
        self.assertAlmostEqual(r["crypto_perp_funding"].prob, 0.5 - 0.015)   # capped
        self.assertAlmostEqual(r["crypto_perp_premium"].prob, 0.5 + 0.015)
        r = self.by_name({"hl_funding": -0.0005, "hl_premium": -0.002})
        self.assertLess(r["crypto_perp_premium"].prob, 0.5)
        self.assertGreater(r["crypto_perp_funding"].prob, 0.5)

    def test_missing_or_bad_data_gives_no_reading(self):
        self.assertEqual(crypto_brain.perp_readings({}, 0.5), [])
        self.assertEqual(crypto_brain.perp_readings({"hl_funding": float("nan"),
                                                     "hl_premium": None}, 0.5), [])
        self.assertEqual(crypto_brain.perp_readings({"hl_funding": 0.0001}, None), [])

    def test_start_with_small_learned_weights(self):
        for name in ("crypto_perp_funding", "crypto_perp_premium"):
            self.assertEqual(brain.DEFAULT_WEIGHTS[name], 0.10)

    def test_bnb_contracts_learn_in_the_crypto_domain(self):
        self.assertEqual(brain.domain_of("KXBNBD-26OCT2717-T745"), "crypto")


if __name__ == "__main__":
    unittest.main()
