import os
import tempfile
import unittest
from unittest.mock import MagicMock

from bot import sports_consistency as sc
from bot.storage import Store


def mk(ticker, bid, ask, title="", strike=None, kind=None, status="active"):
    m = {"ticker": ticker, "status": status, "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
         "yes_sub_title": title}
    if strike is not None:
        m.update(floor_strike=strike, strike_type=kind or "greater")
    return m


class TestWinner(unittest.TestCase):
    def test_a_normal_game_is_nowhere_near_riskless(self):
        ev = {"markets": [mk("A", .30, .54), mk("B", .45, .68)]}                  # the real wide books
        res = sc.winner_check(ev)
        self.assertLess(res["net"], -0.15)

    def test_crossed_asks_below_a_dollar_after_fees_are_found(self):
        ev = {"markets": [mk("A", .40, .45), mk("B", .38, .47)]}                  # asks sum to 0.92
        res = sc.winner_check(ev)
        self.assertGreater(res["net"], 0)
        self.assertEqual(res["side"], "yes")

    def test_crossed_bids_over_a_dollar_after_fees_are_found(self):
        ev = {"markets": [mk("A", .62, .66), mk("B", .50, .55)]}                  # bids sum to 1.12
        res = sc.winner_check(ev)
        self.assertGreater(res["net"], 0)
        self.assertEqual(res["side"], "no")

    def test_missing_quotes_or_odd_outcome_counts_are_skipped(self):
        self.assertIsNone(sc.winner_check({"markets": [mk("A", 0, .5), mk("B", .4, .5)]}))     # no bid is not a price
        self.assertIsNone(sc.winner_check({"markets": [mk("A", .4, .5)]}))
        self.assertIsNone(sc.winner_check({"markets": [mk(str(i), .1, .2) for i in range(5)]}))
        self.assertIsNone(sc.winner_check({"markets": [mk("A", .5, .4), mk("B", .4, .5)]}))   # crossed book is bad data


class TestLadder(unittest.TestCase):
    def rungs(self, spec):
        return [(k, t, b, a) for k, t, b, a in spec]

    def test_grouping_by_player_and_ignoring_unquoted_rungs(self):
        ev = {"markets": [mk("p15", .5, .55, "Ja Morant: 15+", 14.5), mk("p20", .2, .25, "Ja Morant: 20+", 19.5),
                          mk("q15", .5, .55, "Ty Jerome: 15+", 14.5), mk("q20", 0, .25, "Ty Jerome: 20+", 19.5),
                          mk("t", .5, .55, "Over 220.5 points", 220.5), mk("t2", .4, .45, "Over 224.5 points", 224.5)]}
        lad = sc.ladders(ev)
        self.assertEqual(sorted(lad), ["", "Ja Morant"])                          # Jerome has only one quoted rung
        self.assertEqual([r[0] for r in lad["Ja Morant"]], [14.5, 19.5])

    def test_a_normal_ladder_is_not_a_trade(self):
        res = sc.ladder_check([(14.5, "a", .50, .55), (19.5, "b", .20, .25), (24.5, "c", .05, .08)])
        self.assertLess(res["net"], 0)

    def test_a_higher_rung_bid_above_a_lower_rung_ask_is_a_trade_after_fees(self):
        res = sc.ladder_check([(14.5, "a", .38, .40), (19.5, "b", .55, .58)])    # 20+ bid 55 > 15+ ask 40
        self.assertGreater(res["net"], 0.05)
        self.assertEqual((res["low"], res["high"]), ("a", "b"))

    def test_a_tiny_gap_is_eaten_by_fees(self):
        res = sc.ladder_check([(14.5, "a", .48, .50), (19.5, "b", .51, .53)])    # bid 51 > ask 50, one cent, fees bigger
        self.assertLess(res["net"], 0)


class TestCurve(unittest.TestCase):
    def test_isotonic_leaves_a_decreasing_curve_alone_and_pools_a_violation(self):
        self.assertEqual(sc.isotonic_fit([.9, .6, .3, .1]), [.9, .6, .3, .1])
        fit = sc.isotonic_fit([.9, .3, .5, .1])                                   # .3 then .5 rises: pool them
        self.assertEqual(fit, [.9, .4, .4, .1])
        self.assertTrue(all(a >= b - 1e-12 for a, b in zip(fit, fit[1:])))

    def test_gaps_need_four_rungs_and_flag_the_odd_one(self):
        rungs = [(1, "a", .85, .95), (2, "b", .55, .65), (3, "c", .65, .75), (4, "d", .05, .15)]
        gaps = sc.curve_gaps(rungs)
        self.assertEqual(len(gaps), 4)
        self.assertEqual(sc.curve_gaps(rungs[:3]), [])
        self.assertAlmostEqual(gaps[1][5], gaps[2][5])                           # b and c pooled onto one level


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def client(self, events):
        c = MagicMock()
        c._paged = MagicMock(side_effect=lambda path, key, params, max_pages=1: events.get(params["series_ticker"], []))
        return c

    def test_a_pass_counts_checks_keeps_near_misses_and_is_throttled(self):
        good = {"event_ticker": "G1", "markets": [mk("A", .30, .54), mk("B", .45, .68)]}
        bad = {"event_ticker": "G2", "markets": [mk("A", .40, .45), mk("B", .38, .47)]}
        lad = {"event_ticker": "P1", "markets": [mk("a", .38, .40, "X: 15+", 14.5), mk("b", .55, .58, "X: 20+", 19.5)]}
        client = self.client({"KXNBAGAME": [good, bad], "KXNBAPTS": [lad]})
        tally = sc.run(self.store, client, now=1_000_000, every=300, series=["KXNBAGAME", "KXNBAPTS"])
        self.assertEqual((tally["winner_checks"], tally["ladder_checks"], tally["riskless"]), (2, 1, 2))
        rep = sc.report(self.store, now=1_000_000)
        self.assertEqual(rep["kinds"]["winner"]["positive"], 1)
        self.assertEqual(rep["kinds"]["ladder"]["positive"], 1)
        self.assertEqual(rep["kinds"]["winner"]["near"], 1)                      # the ordinary game is far from riskless: not kept
        self.assertIsNone(sc.run(self.store, client, now=1_000_100, every=300, series=["KXNBAGAME"]))   # throttled
        self.assertIsNotNone(sc.run(self.store, client, now=1_000_400, every=300, series=["KXNBAGAME"]))
        self.assertEqual(sc.report(self.store, now=1_000_400)["passes"], 2)

    def test_a_failing_series_does_not_stop_the_others(self):
        client = MagicMock()
        def paged(path, key, params, max_pages=1):
            if params["series_ticker"] == "BAD":
                raise RuntimeError("429")
            return [{"event_ticker": "G", "markets": [mk("A", .40, .45), mk("B", .38, .47)]}]
        client._paged = paged
        tally = sc.run(self.store, client, now=5, every=0, series=["BAD", "KXNBAGAME"])
        self.assertEqual(tally["winner_checks"], 1)

    def test_report_on_an_empty_store_is_safe_and_old_rows_expire(self):
        rep = sc.report(self.store, now=10)
        self.assertEqual((rep["passes"], rep["kinds"]["winner"]["near"]), (0, 0))
        self.store.x("INSERT INTO sports_consistency VALUES(?,?,?,?,?,?)", (1, "winner", "S", "E", 0.01, "{}"))
        self.assertEqual(sc.report(self.store, now=100 * 86400)["kinds"]["winner"]["near"], 0)


if __name__ == "__main__":
    unittest.main()
