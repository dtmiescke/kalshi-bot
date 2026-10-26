import json
import unittest

from bot.learning_impact import report


class MemoryStore:
    def __init__(self):
        self.weights = [
            {"ts": 1_000_000_000_000, "signal": "crypto_model@crypto", "weight": 0.1, "reason": "old"},
            {"ts": 2_000_000_000_000, "signal": "crypto_model@crypto", "weight": 2.0, "reason": "20 markets, mean_rel_logloss=-0.1"},
        ]
        self.decisions = [{
            "id": 1, "ts": 2001, "ticker": "KXBTC15M-TEST", "series": "KXBTC15M",
            "decision": "candidate", "reason": None, "market_price": 0.5,
            "model_prob": 0.7, "side_prob": 0.7, "edge": 0.2,
            "signals_json": json.dumps({
                "kalshi_market": {"prob": 0.5, "confidence": 1.0, "detail": {}},
                "crypto_model": {"prob": 0.9, "confidence": 1.0, "detail": {}},
            }),
        }]

    def current_weights(self):
        out = {}
        for row in self.weights:
            out[row["signal"]] = row["weight"]
        return out

    def q(self, sql, params=()):
        if "FROM weight_history" in sql:
            if "ORDER BY ts DESC LIMIT 500" in sql:
                return sorted(self.weights, key=lambda r: r["ts"], reverse=True)[:500]
            cutoff = params[0]
            latest = {}
            for row in self.weights:
                if row["ts"] <= cutoff and (row["signal"] not in latest or row["ts"] > latest[row["signal"]]["ts"]):
                    latest[row["signal"]] = row
            return [{"signal": r["signal"], "weight": r["weight"]} for r in latest.values()]
        if "FROM decision_audit" in sql:
            start, limit = params
            return [r for r in self.decisions if r["ts"] >= start][:limit]
        raise AssertionError("Unexpected SQL: " + sql)


class LearningImpactTests(unittest.TestCase):
    def test_report_compares_saved_snapshot_using_before_and_after_weights(self):
        result = report(MemoryStore())
        self.assertTrue(result["ok"])
        self.assertEqual(result["snapshots_compared"], 1)
        row = result["snapshots"][0]
        self.assertNotEqual(row["prob_with_pre_update_weights"], row["prob_with_current_weights"])
        self.assertGreater(row["probability_change_pp"], 0)
        self.assertFalse(row["side_changed"])
        self.assertIn("no orders are placed", result["note"])


if __name__ == "__main__":
    unittest.main()
