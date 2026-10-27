"""The trainer's history download is bounded: it skips the archive when recent
markets are found, skips markets it already has, and stops at its deadline so
training always gets to run (train.yml was timing out in the download)."""
import os
import tempfile
import time
import unittest

from bot import history, ml
from bot.storage import Store

NOW = time.time()


def market(i):
    close = NOW - 3600 - i * 900
    return {"ticker": f"KXBTC15M-W{i}-00", "result": "yes", "floor_strike": 100000,
            "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(close)),
            "expiration_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(close))}


class FakeKalshi:
    def __init__(self, n=5):
        self.n, self.paths = n, []

    def _paged(self, path, key, params, max_pages=10):
        self.paths.append(path)
        return [market(i) for i in range(self.n)] if path == "/markets" else []

    def _req(self, method, path, params=None):
        self.paths.append(path)
        return {"candlesticks": []}


class TestHistoryBudget(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def test_archive_skipped_when_recent_markets_found(self):
        c = FakeKalshi()
        got = history.settled_markets(c, "KXBTC15M", NOW - 86400, NOW)
        self.assertEqual(len(got), 5)
        self.assertNotIn("/historical/markets", c.paths)

    def test_archive_still_used_when_nothing_recent(self):
        c = FakeKalshi(n=0)
        history.settled_markets(c, "KXBTC15M", NOW - 86400, NOW)
        self.assertIn("/historical/markets", c.paths)

    def test_already_downloaded_markets_are_skipped(self):
        ml.add_row(self.st, "KXBTC15M-W0-00", 1, "BTC", {"a": 1}, 1, "history")
        c = FakeKalshi()
        history.run(self.st, c, days=1, series=["KXBTC15M"], spot_get=lambda *a, **k: _Empty())
        candle_calls = [p for p in c.paths if "candlesticks" in p]
        self.assertFalse(any("W0-00" in p for p in candle_calls))
        self.assertTrue(any("W1-00" in p for p in candle_calls))

    def test_stops_at_deadline(self):
        c = FakeKalshi()
        out = history.run(self.st, c, days=1, deadline_ts=NOW - 1, spot_get=lambda *a, **k: _Empty())
        self.assertEqual(out["stopped"], "time limit")
        self.assertIn("time limit", history.text(out))
        self.assertFalse([p for p in c.paths if "candlesticks" in p])


class TestSpotCandlesEnds(unittest.TestCase):
    def test_fractional_end_time_terminates(self):
        calls = []

        def get(*a, **k):
            calls.append(k["params"]["start"])
            return _Empty()
        history.spot_candles("BTC-USD", 1_000_000.7, 1_000_000.7 + 600 * 60 + 0.9, get=get)
        self.assertLessEqual(len(calls), 3)              # ~two 300-minute chunks, then done


class _Empty:
    status_code = 200

    def json(self):
        return []

    def raise_for_status(self):
        pass


if __name__ == "__main__":
    unittest.main()
