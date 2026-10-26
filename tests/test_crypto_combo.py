import unittest

from bot import crypto_combo


class FakeStore:
    def __init__(self, values=None, outcomes=None):
        self.values = dict(values or {})
        self.outcomes = list(outcomes or [])

    def get_kv(self, key, default=None):
        return self.values.get(key, default)

    def set_kv(self, key, value):
        self.values[key] = value

    def q(self, query, params=()):
        if "FROM graded_markets" in query:
            return self.outcomes
        return []


class FakeSettings:
    mode = "live"
    taker_fee_rate = 0.07
    max_per_bet = 25


class TestCryptoComboPaperBets(unittest.TestCase):

    def test_dynamic_stake_has_fifteen_dollar_minimum_and_scales_with_edge(self):
        small = crypto_combo._stake_budget(0.06, 0.7, 25)
        large = crypto_combo._stake_budget(0.20, 0.95, 25)
        self.assertGreaterEqual(small, 15.0)
        self.assertGreater(large, small)
        self.assertLessEqual(large, 25.0)

    def test_configured_cap_can_support_minimum_and_larger_crypto_bets(self):
        self.assertEqual(crypto_combo._stake_budget(0.5, 1.0, 25), 25.0)
        self.assertEqual(crypto_combo._stake_budget(0.001, 0.1, 25), 15.0)

    def test_non_paper_mode_never_creates_combo(self):
        store = FakeStore()
        result = crypto_combo.scan(object(), store, settings=FakeSettings(), now=1_800_000_000)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], "paper_mode_only")
        self.assertEqual(store.get_kv("crypto_combo_bets", []), [])

    def test_grades_only_after_all_three_legs_settle(self):
        legs = [
            {"ticker": "KXBTC15M-A", "side": "yes"},
            {"ticker": "KXETH15M-B", "side": "no"},
            {"ticker": "KXSOL15M-C", "side": "yes"},
        ]
        combos = [
            {"id": "WIN", "fingerprint": "w", "ts": 1, "legs": legs,
             "contracts": 10, "price": 0.12, "fee": 0.03,
             "predicted_yes": 0.4, "status": "open"},
            {"id": "WAIT", "fingerprint": "x", "ts": 2, "legs": legs,
             "contracts": 10, "price": 0.12, "fee": 0.03,
             "predicted_yes": 0.4, "status": "open"},
        ]
        store = FakeStore(
            {"crypto_combo_bets": combos},
            [
                {"ticker": "KXBTC15M-A", "outcome": 1},
                {"ticker": "KXETH15M-B", "outcome": 0},
                {"ticker": "KXSOL15M-C", "outcome": 1},
            ],
        )
        summary = crypto_combo.grade(store, now=1_800_000_000)
        saved = store.get_kv("crypto_combo_bets")
        self.assertEqual(summary["graded"], 2)
        self.assertEqual(saved[0]["outcome"], 1)
        self.assertAlmostEqual(saved[0]["pnl"], 8.77)
        self.assertEqual(saved[1]["status"], "graded")

    def test_combo_waits_for_every_component_outcome(self):
        combo = {"id": "WAIT", "fingerprint": "x", "ts": 2,
                 "legs": [{"ticker": "KXBTC15M-A", "side": "yes"},
                          {"ticker": "KXETH15M-B", "side": "yes"},
                          {"ticker": "KXSOL15M-C", "side": "yes"}],
                 "contracts": 2, "price": 0.1, "fee": 0.01,
                 "predicted_yes": 0.2, "status": "open"}
        store = FakeStore({"crypto_combo_bets": [combo]}, [
            {"ticker": "KXBTC15M-A", "outcome": 1},
            {"ticker": "KXETH15M-B", "outcome": 1},
        ])
        summary = crypto_combo.grade(store, now=1_800_000_000)
        self.assertEqual(summary["graded"], 0)
        self.assertEqual(store.get_kv("crypto_combo_bets")[0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
