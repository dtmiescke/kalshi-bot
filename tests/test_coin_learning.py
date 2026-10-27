import json
import unittest
from bot.coin_learning import (build_trade_review, coin_from_market, coin_performance, format_coin_report, learning_summary)

class CoinLearningTests(unittest.TestCase):
    def test_coin_mapping_handles_all_four_crypto_series(self):
        self.assertEqual([coin_from_market("KX" + c + "15M", "") for c in ("BTC", "BNB", "SOL", "ETH")],
                         ["BTC", "BNB", "SOL", "ETH"])

    def test_coin_ranking_is_net_profit_best_to_worst(self):
        rows = [
            {"series": "KXBTC15M", "ticker": "BTC", "side": "yes", "result": "yes", "pnl": 8, "stake": 20},
            {"series": "KXETH15M", "ticker": "ETH", "side": "yes", "result": "no", "pnl": -4, "stake": 20},
            {"series": "KXBNB15M", "ticker": "BNB", "side": "no", "result": "no", "pnl": 2, "stake": 10},
        ]
        stats = coin_performance(rows)
        self.assertEqual([x["coin"] for x in stats], ["BTC", "BNB", "ETH"])
        self.assertEqual([x["net_profit"] for x in stats], [8, 2, -4])
        self.assertAlmostEqual(stats[0]["roi_on_stake"], .4)
        self.assertIn("1. BTC: net profit $8.00", format_coin_report({"graded_bets": 3, "coins": stats}))

    def test_review_separates_facts_from_unproven_causal_hypotheses(self):
        review = build_trade_review({
            "id": 1, "ticker": "KXBTC15M-X", "series": "KXBTC15M", "mode": "paper",
            "side": "yes", "result": "no", "pnl": -5, "price": .65, "contracts": 10,
            "fee": .1, "model_prob": .8, "confidence": .8, "edge": .15, "clv": -.03,
            "signals_json": json.dumps({"chart_setups": ["breakout"], "order_book": {"imbalance": .7}}),
        })
        self.assertEqual(review["observed"]["outcome"], "loss")
        self.assertEqual(review["observed"]["coin"], "BTC")
        self.assertIn("chart_setups", review["observed"]["signals_used"])
        self.assertTrue(review["hypotheses_not_proven"])
        self.assertIn("caused", review["lesson"].lower())

    def test_learning_summary_reports_associations_as_low_sample_without_overclaiming(self):
        class Store:
            def q(self, sql, args=()):
                return [
                    {"review_json": json.dumps({"observed": {"signals_used": ["order_book"], "pnl": 2.0}})},
                    {"review_json": json.dumps({"observed": {"signals_used": ["order_book"], "pnl": -1.0}})},
                ]
        rows = learning_summary(Store(), min_samples=20)
        self.assertEqual(rows[0]["samples"], 2)
        self.assertEqual(rows[0]["net_profit"], 1.0)
        self.assertEqual(rows[0]["evidence_level"], "low_sample")

    def test_missing_signal_context_is_explicit(self):
        review = build_trade_review({"ticker": "KXETH15M-X", "side": "no", "result": "yes", "pnl": -1})
        self.assertIn("under-documented", " ".join(review["hypotheses_not_proven"]))

if __name__ == "__main__":
    unittest.main()
