"""T1c: read-only check of our spot price vs Kalshi's settlement index."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from bot import grader
from bot.storage import Store

CLOSE = 1_800_000_900          # a 15-minute boundary


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeClient:
    def __init__(self, markets):
        self.markets = markets
        self.calls = []

    def market(self, ticker):
        self.calls.append(ticker)
        if ticker not in self.markets:
            raise RuntimeError("404")
        return self.markets[ticker]

    def _paged(self, path, key, params, max_pages=20):
        if path != "/markets":
            return []
        return [m for m in self.markets.values() if m["ticker"].startswith(params["series_ticker"])]


class TestBotReadingsGap(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def reading(self, ticker, ts, spot, secs_left):
        self.st.add_signal(ts, ticker, "crypto_model", 0.5, 0.5,
                           {"spot": spot, "secs_left": secs_left, "avg_s": 60})

    def test_gap_from_final_minute_readings(self):
        t = "KXBTC15M-W1-00"
        self.st.set_kv(f"start_{t}", CLOSE)
        self.reading(t, CLOSE - 300, 99000.0, 300)        # outside the averaging minute: ignored
        self.reading(t, CLOSE - 50, 100010.0, 50)
        self.reading(t, CLOSE - 20, 100030.0, 20)
        client = FakeClient({t: {"ticker": t, "result": "yes", "expiration_value": "100000.00"}})
        report = grader.settlement_gap(self.st, client, now=CLOSE + 600)
        btc = report["by_source"]["bot_readings"]["BTC"]
        self.assertEqual(btc["n"], 1)
        self.assertAlmostEqual(btc["mean_bps"], 2.0)       # mean 100020 vs 100000
        self.assertIsNone(btc["recommended_proxy_bps"])    # too few samples to recommend
        self.assertEqual(self.st.get_kv("settle_gap_report")["samples"], 1)
        # already measured: not fetched again
        grader.settlement_gap(self.st, client, now=CLOSE + 900)
        self.assertEqual(client.calls, [t])

    def test_skips_unsettled_missing_and_failed(self):
        for t in ("KXBTC15M-A-00", "KXBTC15M-B-00", "KXBTC15M-C-00", "KXETH15M-D-00"):
            self.st.set_kv(f"start_{t}", CLOSE)
        self.reading("KXBTC15M-A-00", CLOSE - 10, 100.0, 10)    # no expiration value
        self.reading("KXBTC15M-B-00", CLOSE - 10, 100.0, 10)    # lookup fails
        self.reading("KXETH15M-D-00", CLOSE - 400, 100.0, 400)  # no final-minute reading
        client = FakeClient({"KXBTC15M-A-00": {"ticker": "KXBTC15M-A-00", "result": "",
                                               "expiration_value": ""}})
        report = grader.settlement_gap(self.st, client, now=CLOSE + 600)
        self.assertEqual(report["skipped_this_run"], {"no_final_minute_readings": 1,
                                                      "no_expiration_value": 1, "lookup_failed": 1})
        self.assertEqual(report["samples"], 0)
        # not yet settled (inside the 5-minute grace): not even looked up
        client.calls.clear()
        self.st.set_kv("start_KXBTC15M-E-00", CLOSE)
        self.reading("KXBTC15M-E-00", CLOSE - 10, 100.0, 10)
        grader.settlement_gap(self.st, client, now=CLOSE + 60)
        self.assertNotIn("KXBTC15M-E-00", client.calls)

    def test_recommends_rms_with_enough_samples(self):
        samples = [{"source": "coinbase_replay", "ticker": f"T{i}", "coin": "BTC",
                    "close_ts": i, "gap_bps": 2.0 if i % 2 else -2.0} for i in range(40)]
        summary = grader._settle_gap_summary(samples)["coinbase_replay"]["BTC"]
        self.assertEqual(summary["n"], 40)
        self.assertAlmostEqual(summary["mean_bps"], 0.0)
        self.assertEqual(summary["recommended_proxy_bps"], 2.0)

    def test_does_not_change_any_setting(self):
        before = dict(os.environ)
        grader.settlement_gap(self.st, FakeClient({}), now=CLOSE)
        self.assertEqual(dict(os.environ), before)
        self.assertIsNone(self.st.get_kv("SETTLE_PROXY_BPS"))


class TestReplayGap(unittest.TestCase):
    def test_coinbase_final_minute_vs_expiration_value(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            m = {"ticker": "KXBTC15M-W-00", "result": "yes", "close_time": iso(CLOSE),
                 "floor_strike": 99000, "expiration_value": "100000",
                 "rules_primary": "average of the 60 seconds prior"}
            client = FakeClient({m["ticker"]: m})

            class Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    # [ts, low, high, open, close, volume]; final minute starts CLOSE-60
                    return [[CLOSE - 60, 99990, 100030, 100000, 100020, 5],
                            [CLOSE - 120, 1, 1, 1, 1, 1]]

            report = grader.replay_settlement_gap(st, client, days=1, now=CLOSE + 600,
                                                  get=lambda *a, **k: Resp())
            btc = report["by_source"]["coinbase_replay"]["BTC"]
            self.assertEqual(btc["n"], 1)
            self.assertAlmostEqual(btc["mean_bps"], 1.0)      # OHLC mean 100010 vs 100000
            # a second run doesn't duplicate the window
            report = grader.replay_settlement_gap(st, client, days=1, now=CLOSE + 600,
                                                  get=lambda *a, **k: Resp())
            self.assertEqual(report["samples"], 1)
            st.db.close()


if __name__ == "__main__":
    unittest.main()
