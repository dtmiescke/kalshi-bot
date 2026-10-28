"""BNB history for the trainer comes from OKX (no Coinbase product; Binance
returns 451 from US hosts). Paging walks backwards and stops at the start."""
import unittest

from bot import history


class Resp:
    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"code": "0", "data": self.rows}


class TestOkxHistory(unittest.TestCase):
    def test_bnb_series_routes_to_okx(self):
        self.assertEqual(history.SERIES_PAIR["KXBNB15M"], "OKX:BNB-USDT")
        calls = []
        history.spot_candles("OKX:BNB-USDT", 0, 60, get=lambda u, **k: calls.append((u, k["params"])) or Resp([]))
        self.assertEqual(calls[0][0], history.OKX_CANDLES)
        self.assertEqual(calls[0][1]["instId"], "BNB-USDT")

    def test_pages_backwards_until_start(self):
        start, end = 1_000_000 * 60, 1_000_000 * 60 + 250 * 60      # 251 minutes
        calls = []

        def get(url, params=None, **kw):
            calls.append(params["after"])
            newest = min(params["after"] // 1000 - 60, end)
            rows = [[str((newest - i * 60) * 1000), "700", "702", "699", "701", "3.5", "0", "0", "1"]
                    for i in range(100)]
            return Resp(rows)
        orig = history.time.sleep
        history.time.sleep = lambda s: None
        try:
            got = history.okx_candles("BNB-USDT", start, end, get=get)
        finally:
            history.time.sleep = orig
        self.assertEqual(len(got), 251)
        self.assertEqual(min(got), start)
        self.assertEqual(max(got), end)
        self.assertEqual(got[start], {"ts": start, "open": 700.0, "high": 702.0, "low": 699.0,
                                      "close": 701.0, "volume": 3.5})
        self.assertEqual(len(calls), 3)                     # 100 + 100 + 51 minutes
        self.assertTrue(all(a > b for a, b in zip(calls, calls[1:])))

    def test_outage_returns_what_it_has(self):
        def boom(*a, **k):
            raise ConnectionError("down")
        self.assertEqual(history.okx_candles("BNB-USDT", 0, 6000, get=boom), {})


if __name__ == "__main__":
    unittest.main()
