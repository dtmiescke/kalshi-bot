import os
import tempfile
import unittest
from unittest.mock import MagicMock

from bot import nba_tonight, nfl_tonight, pick_quotes, shadow_monitor
from bot.storage import Store


def add(store, table, ticker, status="open", side="yes", price=0.4):
    store.x(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, 1, "A at B", "prop", f"{ticker} 5+ points ({side.upper()})", side, price, 10, 4.0, 0.2, 0.6, 0.1, 2000, status, None,
             None, None))


class TestQuotes(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        self.store.x(nba_tonight.SCHEMA)
        self.store.x(nfl_tonight.SCHEMA)

    def client(self, quotes):
        c = MagicMock()
        c._paged = MagicMock(side_effect=lambda path, key, params, max_pages=1: [
            {"ticker": t, "yes_bid_dollars": f"{q[0]:.4f}", "yes_ask_dollars": f"{q[1]:.4f}"}
            for t in params["tickers"].split(",") for q in [quotes[t]] if t in quotes])
        return c

    def test_only_open_picks_are_quoted_in_batches_and_throttled(self):
        for i in range(pick_quotes.BATCH + 5):
            add(self.store, "nba_picks", f"N{i:03d}")
        add(self.store, "nfl_picks", "F1")
        add(self.store, "nba_picks", "DONE", status="settled")
        c = self.client({f"N{i:03d}": (0.4, 0.5) for i in range(pick_quotes.BATCH + 5)} | {"F1": (0.1, 0.2), "DONE": (0.9, 0.95)})
        self.assertEqual(pick_quotes.refresh(self.store, c, now=1000, every=300), pick_quotes.BATCH + 6)
        self.assertEqual(c._paged.call_count, 2)                                    # 56 tickers: two batches
        q = self.store.get_kv("pick_quotes")
        self.assertNotIn("DONE", q)
        self.assertEqual(q["F1"], {"bid": 0.1, "ask": 0.2, "ts": 1000})
        self.assertIsNone(pick_quotes.refresh(self.store, c, now=1100, every=300))   # throttled
        self.assertEqual(c._paged.call_count, 2)

    def test_a_failed_batch_keeps_the_others_and_never_raises(self):
        add(self.store, "nba_picks", "N1")
        c = MagicMock()
        c._paged = MagicMock(side_effect=RuntimeError("429"))
        self.assertEqual(pick_quotes.refresh(self.store, c, now=1000, every=0), 0)
        self.assertIsNone(self.store.get_kv("pick_quotes"))                          # nothing quoted: the old quotes stay

    def test_monitor_shows_the_chance_for_the_side_bought_and_the_payouts(self):
        add(self.store, "nba_picks", "Y1", side="yes", price=0.40)
        add(self.store, "nba_picks", "N1", side="no", price=0.60)
        self.store.set_kv("pick_quotes", {"Y1": {"bid": 0.50, "ask": 0.60, "ts": 5}, "N1": {"bid": 0.50, "ask": 0.60, "ts": 5}})
        rows = {p["id"]: p for p in shadow_monitor.report(self.store)["picks"]}
        self.assertEqual(rows["Y1"]["now"], 0.55)                                    # YES mid
        self.assertEqual(rows["N1"]["now"], 0.45)                                    # NO side: one minus the YES mid
        self.assertEqual((rows["Y1"]["contracts"], rows["Y1"]["stake"]), (10, 4.0))
        self.assertEqual(rows["Y1"]["win"], round(10 * 0.60 - 0.2, 2))              # 10 contracts bought at 40: wins $6 less the fee
        self.assertEqual(rows["Y1"]["lose"], 4.2)

    def test_combo_chance_now_is_the_legs_multiplied_and_missing_quotes_give_none(self):
        import json
        from bot import sports_combos
        self.store.x(sports_combos.SCHEMA)
        add(self.store, "nba_picks", "A1", side="yes", price=0.5)
        add(self.store, "nba_picks", "B1", side="no", price=0.5)
        legs = [{"ticker": "A1", "label": "A1", "side": "yes", "price": .5, "p_model": .7, "table": "nba_picks"},
                {"ticker": "B1", "label": "B1", "side": "no", "price": .5, "p_model": .7, "table": "nba_picks"}]
        self.store.x("INSERT INTO sports_combos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("K", 1, 2, json.dumps(legs), .25, .49, .36, .1, 20, 5.0, .1, 9, "open", None, None))
        self.store.set_kv("pick_quotes", {"A1": {"bid": .6, "ask": .6, "ts": 1}})
        self.assertIsNone(shadow_monitor.report(self.store)["combos"][0]["now"])        # one leg unquoted
        self.store.set_kv("pick_quotes", {"A1": {"bid": .6, "ask": .6, "ts": 1}, "B1": {"bid": .7, "ask": .7, "ts": 1}})
        c = shadow_monitor.report(self.store)["combos"][0]
        self.assertAlmostEqual(c["now"], .6 * .3)
        self.assertEqual((c["win"], c["lose"], len(c["id"])), (round(20 * .75 - .1, 2), 5.1, 10))


if __name__ == "__main__":
    unittest.main()
