"""The lead-lag study records spot jumps and how fast Kalshi followed; it never bets."""
import os
import tempfile
import unittest

from bot import lead_lag
from bot.storage import Store

SIGMA = 0.0006          # per minute; a 5-second sd is 0.0006 * sqrt(5/60) = 0.000173


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.ll = lead_lag.LeadLag(self.st)

    def tearDown(self):
        self.d.cleanup()

    def run_path(self, seconds, spot, mid, model, start=1_000_000):
        for i in range(seconds):
            m = mid(i)
            self.ll.observe("KXBTC15M-T1", "KXBTC15M", start + i, spot(i), m - .01, m + .01, model(i), SIGMA)


class TestJumps(Base):
    def test_a_calm_market_records_nothing(self):
        self.run_path(60, lambda i: 100_000 * (1 + 0.00002 * (i % 3)), lambda i: .5, lambda i: .5)
        self.assertEqual(self.ll.pending, [])
        self.assertEqual(self.st.q("SELECT name FROM sqlite_master WHERE name='lead_lag'"), [])

    def test_a_jump_is_followed_until_every_horizon_is_filled_then_stored(self):
        # spot is flat for 10 s, jumps 0.05% (about 3 sd) up, then holds; Kalshi catches up over a minute
        spot = lambda i: 100_000 if i < 10 else 100_050
        model = lambda i: .50 if i < 10 else .70
        mid = lambda i: .50 if i < 10 else min(.70, .50 + .004 * (i - 10))
        self.run_path(90, spot, mid, model)
        rows = self.st.q("SELECT * FROM lead_lag")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["direction"], 1)
        self.assertGreater(r["z"], 2.0)
        self.assertAlmostEqual((r["bid0"] + r["ask0"]) / 2, .50, places=1)
        import json
        f = json.loads(r["follow"])
        self.assertEqual(sorted(map(int, f)), list(lead_lag.HORIZONS))
        self.assertLess(f["2"][0], f["30"][0])                 # Kalshi's mid was still catching up
        self.assertEqual(f["60"][1], .70)                       # the formula had already moved

    def test_one_jump_is_not_recorded_twice_inside_the_cooldown(self):
        spot = lambda i: 100_000 if i < 10 else 100_050 if i < 20 else 100_100       # two jumps 10 s apart
        self.run_path(40, spot, lambda i: .5, lambda i: .5)
        self.assertEqual(len(self.ll.pending), 1)               # still collecting follow-ups for the first
        self.run_path(60, lambda i: 100_100, lambda i: .5, lambda i: .5, start=1_000_040)
        self.assertEqual(self.st.q("SELECT COUNT(*) c FROM lead_lag")[0]["c"], 1)

    def test_bad_inputs_never_raise_into_the_trading_loop(self):
        self.ll.observe("T", "S", 1.0, None, None, None, None, None)
        self.ll.observe("T", "S", 2.0, 100_000, .4, .5, .5, 0)
        self.ll.observe("T", "S", 3.0, "oops", .4, .5, .5, SIGMA)


class TestReport(Base):
    def add(self, ticker, direction, ask0, bid0, follow, outcome):
        import json
        lead_lag.ensure_table(self.st)
        self.st.x("INSERT INTO lead_lag VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (ticker, "KXBTC15M", 1_000_000 + len(ticker) + hash(ticker) % 1000, direction, 2.5, 100_000, bid0, ask0, .5, json.dumps(follow)))
        self.st.x("INSERT OR IGNORE INTO graded_markets VALUES(?,?,?,?)", (ticker, 0, outcome, "KXBTC15M"))

    def test_a_delayed_order_gets_the_later_worse_quote(self):
        for i in range(30):
            # YES jump: the ask was 51c when spotted but Kalshi had caught up to 60c two seconds later; YES won
            self.add(f"KXBTC15M-D{i}", 1, .51, .49, {"2": [.595, .70, .59, .60]}, 1)
        r = lead_lag.report(self.st, now=1_000_100)
        self.assertEqual(r["delayed"]["latency_s"], lead_lag.LATENCY_S)
        self.assertEqual(r["delayed"]["settled"], 30)
        self.assertGreater(r["mean_pnl"], r["delayed"]["mean_pnl"])          # waiting cost the early price
        self.assertAlmostEqual(r["mean_pnl"] - r["delayed"]["mean_pnl"], .09, delta=.02)

    def test_older_rows_without_a_later_quote_are_left_out_of_the_delayed_figure(self):
        for i in range(10):
            self.add(f"KXBTC15M-O{i}", 1, .51, .49, {"2": [.56, .70]}, 1)     # recorded before bid/ask were kept
        r = lead_lag.report(self.st, now=1_000_100)
        self.assertEqual((r["settled"], r["delayed"]["settled"]), (10, 0))
        self.assertIsNone(r["delayed"]["mean_pnl"])

    def test_empty_report_is_safe(self):
        r = lead_lag.report(self.st, now=1_000_100)
        self.assertEqual((r["events"], r["settled"], r["mean_pnl"]), (0, 0, None))

    def test_slope_shows_how_much_of_the_formulas_move_kalshi_had_made(self):
        for i in range(12):
            # formula moved +.20; Kalshi had made +.06 after 2 s and +.18 after 30 s
            self.add(f"KXBTC15M-S{i}", 1, .51, .49, {"2": [.56, .70], "30": [.68, .70]}, 1)
        r = lead_lag.report(self.st, now=1_000_100)
        self.assertAlmostEqual(r["follow"]["2"]["slope"], .30, places=1)
        self.assertAlmostEqual(r["follow"]["30"]["slope"], .90, places=1)
        self.assertIsNone(r["follow"]["5"])                    # nothing recorded at that horizon

    def test_trade_pnl_is_net_of_fees_and_uses_the_jumps_direction(self):
        for i in range(30):
            self.add(f"KXBTC15M-W{i}", 1, .50, .48, {}, 1)      # bought YES at 50c and it won
        for i in range(30):
            self.add(f"KXBTC15M-L{i}", -1, .52, .50, {}, 1)     # bought NO at 50c and it lost
        r = lead_lag.report(self.st, now=1_000_100)
        self.assertEqual(r["settled"], 60)
        self.assertAlmostEqual(r["mean_pnl"], (0.4325 + (-0.5175)) / 2 - 0.0, delta=0.03)
        self.assertIsNotNone(r["z"])


if __name__ == "__main__":
    unittest.main()


class TestPerpLead(unittest.TestCase):
    def setUp(self):
        from bot.live import feeds
        self.feeds = feeds
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def test_hyperliquid_mids_update_only_the_coins_followed(self):
        perp = self.feeds.PerpMids(("BTC", "ETH"))
        self.feeds.handle_hyperliquid({"channel": "allMids", "data": {"mids": {"BTC": "81000.5", "ETH": "2300", "DOGE": "0.1", "@5": "1"}}}, perp, now=100.0)
        self.assertEqual(perp.mid("BTC", now=101.0), 81000.5)
        self.assertEqual(perp.mid("ETH", now=101.0), 2300.0)
        self.assertIsNone(perp.mid("DOGE", now=101.0))
        self.assertIsNone(perp.mid("BTC", now=110.0))                       # stale after 5 s: never study on an old quote
        self.feeds.handle_hyperliquid({"channel": "subscriptionResponse"}, perp, now=102.0)    # other messages ignored
        self.feeds.handle_hyperliquid("junk", perp, now=102.0)
        self.assertEqual(self.feeds.hyperliquid_subs()[0]["subscription"]["type"], "allMids")

    def run_study(self, spot_moves):
        ll = lead_lag.LeadLag(self.store, table="perp_lead")
        t0, perp, spot = 1_000.0, 100.0, 100.0
        for i in range(0, 100):
            now = t0 + i
            if i >= 20:
                perp = 100.0 * 1.003                       # a perp jump of 0.3% (about 3 sigma at 0.05%/min) at second 20
                if spot_moves:
                    spot = 100.0 * 1.003
            ll.observe("KXBTC15M-26OCT282345-45", "KXBTC15M", now, perp, 0.50, 0.52, 0.55, 0.0005, ref=spot)
        return ll

    def test_a_perp_jump_with_flat_spot_is_recorded_as_unmoved(self):
        self.run_study(spot_moves=False)
        self.store.x("INSERT INTO graded_markets VALUES(?,?,?,?)", ("KXBTC15M-26OCT282345-45", 1, 1, "live"))
        rep = lead_lag.report(self.store, now=1_100.0, table="perp_lead")
        self.assertEqual(rep["events"], 1)
        self.assertEqual(rep["unmoved"]["events"], 1)
        self.assertEqual(rep["unmoved"]["of"], 1)

    def test_a_jump_spot_also_made_is_not_counted_as_unmoved(self):
        self.run_study(spot_moves=True)
        rep = lead_lag.report(self.store, now=1_100.0, table="perp_lead")
        self.assertEqual(rep["events"], 1)
        self.assertEqual(rep["unmoved"]["events"], 0)
        self.assertEqual(rep["unmoved"]["of"], 1)

    def test_perp_and_spot_studies_use_separate_tables(self):
        self.run_study(spot_moves=False)
        self.assertEqual(lead_lag.report(self.store, now=1_100.0)["events"], 0)         # the spot table is untouched
