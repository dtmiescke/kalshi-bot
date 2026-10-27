import unittest
from unittest import mock

from bot import marketdata


class _Store:
    def __init__(self):
        self.values = {}

    def get_kv(self, key):
        return self.values.get(key)

    def set_kv(self, key, value):
        self.values[key] = value


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TestSpot15mMonitor(unittest.TestCase):
    def test_only_completed_coinbase_candle_is_returned(self):
        candle = marketdata._completed_15m_candle([
            [900, 9, 11, 10, 10.5, 100],
            [1800, 10, 12, 10.5, 11, 120],
        ], "coinbase", now=2000)
        self.assertEqual(candle["ts"], 900)
        self.assertAlmostEqual(candle["return_pct"], 5.0)
        self.assertEqual(candle["volume"], 100)

    def test_kraken_15m_pair_and_completed_bar_are_parsed(self):
        payload = {"error": [], "result": {"XBTUSD": [
            [900, "10", "11", "9", "10.5", "10.2", "100", 25],
            [1800, "10.5", "12", "10", "11", "10.8", "120", 30],
        ], "last": 1800}}
        get = mock.Mock(return_value=_Response(payload))
        expected = {"ts": 900, "open": 10.0, "high": 11.0, "low": 9.0,
                    "close": 10.5, "volume": 100.0, "return_pct": 5.0,
                    "age_seconds": 200.0, "source": "kraken"}
        with mock.patch("bot.marketdata._completed_15m_candle", return_value=expected) as parse:
            candle = marketdata._fetch_kraken_15m("BTC", get=get, now=2000)
        self.assertEqual(candle["source"], "kraken")
        self.assertEqual(parse.call_args.args[0], payload["result"]["XBTUSD"])
        self.assertEqual(parse.call_args.args[1], "kraken")
        self.assertEqual(get.call_args.kwargs["params"], {"pair": "XBTUSD", "interval": 15})

    def test_bnb_is_not_falsely_mapped_to_coinbase(self):
        self.assertIsNone(marketdata._fetch_coinbase_15m("BNB", get=mock.Mock()))

    def test_monitor_includes_both_venues_and_cross_venue_basis(self):
        store = _Store()
        cb = {"ts": 900, "open": 100, "high": 102, "low": 99, "close": 101,
              "volume": 12, "return_pct": 1.0, "age_seconds": 100, "source": "coinbase"}
        kr = {"ts": 900, "open": 100, "high": 101, "low": 98, "close": 100,
              "volume": 20, "return_pct": 0.0, "age_seconds": 100, "source": "kraken"}
        with mock.patch("bot.marketdata._fetch_coinbase_15m", return_value=cb):
            with mock.patch("bot.marketdata._fetch_kraken_15m", return_value=kr):
                result = marketdata.spot_15m_monitor(store, "BTC")
        self.assertEqual(result["timeframe_minutes"], 15)
        self.assertEqual(result["coinbase"], cb)
        self.assertEqual(result["kraken"], kr)
        self.assertAlmostEqual(result["cross_venue_basis_bps"], 100.0)


if __name__ == "__main__":
    unittest.main()
