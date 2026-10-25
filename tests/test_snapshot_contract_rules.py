import unittest

from bot.engine import Engine


class MemoryStore:
    def __init__(self):
        self.snapshot = None

    def add_market_snapshot(self, row):
        self.snapshot = dict(row)


class TestSnapshotContractRules(unittest.TestCase):
    def test_snapshot_preserves_rules_for_counsel_without_changing_prices(self):
        engine = Engine.__new__(Engine)
        engine.store = MemoryStore()
        market = {
            "ticker": "KXNFLGAME-TEST",
            "yes_bid": 45,
            "yes_ask": 48,
            "last_price": 46,
            "rules_primary": "Resolves using the official league result.",
            "rules_secondary": "A postponed game may be void.",
            "early_close_condition": "May close early if the event is decided.",
            "settlement_source": "Official league website",
            "settlement_value": "Final score",
            "custom_strike": None,
        }
        result = engine.snapshot(
            {"event_ticker": "KXNFLGAME-TEST", "series_ticker": "KXNFLGAME",
             "title": "Team A vs Team B"},
            market,
            now=123,
        )
        self.assertEqual(result["yes_bid"], 0.45)
        self.assertEqual(result["yes_ask"], 0.48)
        self.assertEqual(result["rules_primary"], market["rules_primary"])
        self.assertEqual(result["rules_secondary"], market["rules_secondary"])
        self.assertEqual(result["settlement_source"], market["settlement_source"])
        self.assertEqual(engine.store.snapshot["ticker"], "KXNFLGAME-TEST")

    def test_rule_text_is_bounded_in_candidate_context(self):
        engine = Engine.__new__(Engine)
        engine.store = MemoryStore()
        result = engine.snapshot(
            {"event_ticker": "EVT", "series_ticker": "KXNFLGAME"},
            {"ticker": "MKT", "rules_primary": "R" * 5000},
            now=123,
        )
        self.assertEqual(len(result["rules_primary"]), 3000)


if __name__ == "__main__":
    unittest.main()
