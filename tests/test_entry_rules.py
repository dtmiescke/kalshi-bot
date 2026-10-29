"""Entry-rule comparison: each rule only lets through entries that match its condition, and counts them honestly."""
import json
import os
import tempfile
import unittest

from bot import entry_rules
from bot.storage import Store


class TestAccepts(unittest.TestCase):
    def test_baseline_takes_everything(self):
        self.assertTrue(entry_rules.accepts("first_clear", True, 0.5, 12.0, {}))

    def test_even_prices_are_skipped(self):
        self.assertFalse(entry_rules.accepts("away_from_even", True, 0.52, 9.0, {}))
        self.assertTrue(entry_rules.accepts("away_from_even", True, 0.30, 9.0, {}))

    def test_candle_rule_needs_five_minutes_in_and_agreement(self):
        up = {"ret_5m": 0.8}
        self.assertTrue(entry_rules.accepts("candle_5m", True, .5, 9.0, up))
        self.assertFalse(entry_rules.accepts("candle_5m", True, .5, 12.0, up))     # candle not closed yet
        self.assertFalse(entry_rules.accepts("candle_5m", False, .5, 9.0, up))     # betting against the candle
        self.assertTrue(entry_rules.accepts("candle_5m", False, .5, 9.0, {"ret_5m": -0.3}))
        self.assertFalse(entry_rules.accepts("candle_5m", True, .5, 9.0, {}))      # missing data never passes

    def test_strike_and_momentum_rules_are_stricter(self):
        ind = {"ret_5m": 1, "ret_15m": 1, "dist_sigma": 1}
        self.assertTrue(entry_rules.accepts("candle_5m_vs_strike", True, .5, 9.0, ind))
        self.assertFalse(entry_rules.accepts("candle_5m_vs_strike", True, .5, 9.0, {**ind, "dist_sigma": -1}))
        self.assertTrue(entry_rules.accepts("momentum_agree", True, .5, 6.0, ind))
        self.assertFalse(entry_rules.accepts("momentum_agree", True, .5, 9.0, ind))
        self.assertFalse(entry_rules.accepts("momentum_agree", True, .5, 6.0, {**ind, "ret_15m": -1}))


class TestTable(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        self.store = Store(self.path)

    def add(self, ticker, ts, p, k, y, ind):
        self.store.q("""INSERT INTO decision_audit (ts, ticker, event_ticker, series, decision, market_price, model_prob,
                        outcome, brier, signals_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (ts, ticker, ticker, "KXBTC15M", "research", k, p, y, 0.1, json.dumps({"indicators": ind})))

    def test_empty_store_lists_every_rule_as_learning(self):
        t = entry_rules.table(self.store, now=entry_rules.entry_timing.close_ts("KXBTC15M-26OCT282345-45"))
        self.assertEqual(t["windows"], 0)
        self.assertEqual([r["rule"] for r in t["rules"]], list(entry_rules.RULES))
        self.assertTrue(all(r["state"] == "learning" and r["n"] == 0 for r in t["rules"]))

    def test_a_window_is_entered_early_by_baseline_and_late_by_candle_rule(self):
        close = entry_rules.entry_timing.close_ts("KXBTC15M-26OCT282345-45")
        ind_early, ind_late = {"ret_5m": -1.0}, {"ret_5m": 1.0}
        self.add("KXBTC15M-26OCT282345-45", close - 12 * 60, 0.70, 0.55, 1, ind_early)    # edge clears, candle disagrees
        self.add("KXBTC15M-26OCT282345-45", close - 8 * 60, 0.72, 0.55, 1, ind_late)      # edge clears, candle agrees
        t = entry_rules.table(self.store, now=close)
        by = {r["rule"]: r for r in t["rules"]}
        self.assertEqual(t["windows"], 1)
        self.assertEqual(by["first_clear"]["n"], 1)
        self.assertEqual(by["candle_5m"]["n"], 1)
        self.assertEqual(by["momentum_agree"]["n"], 0)                                      # needs ret_15m and strike too
        self.assertGreater(by["first_clear"]["mean"], 0)                                    # YES won at 56¢ + fee
        self.assertEqual(by["first_clear"]["state"], "learning")                            # one window proves nothing

    def test_persistent_edge_needs_the_same_side_on_two_readings_in_a_row(self):
        close = entry_rules.entry_timing.close_ts("KXBTC15M-26OCT282345-45")
        # window A: YES clears the bar at two readings in a row -> entered at the second
        self.add("KXBTC15M-26OCT282345-45", close - 9 * 60, 0.72, 0.55, 1, {})
        self.add("KXBTC15M-26OCT282345-45", close - 8 * 60, 0.72, 0.55, 1, {})
        # window B: clears once, then the edge disappears -> never entered
        self.add("KXETH15M-26OCT282345-45", close - 9 * 60, 0.72, 0.55, 0, {})
        self.add("KXETH15M-26OCT282345-45", close - 8 * 60, 0.55, 0.55, 0, {})
        t = entry_rules.table(self.store, now=close)
        by = {r["rule"]: r for r in t["rules"]}
        self.assertEqual(by["first_clear"]["n"], 2)
        self.assertEqual(by["persistent_edge"]["n"], 1)
        self.assertGreater(by["persistent_edge"]["mean"], 0)                                # window A's YES won


if __name__ == "__main__":
    unittest.main()
