import json
import unittest
from bot.live.adaptive_learning import historical_setup_penalty
from bot.coin_learning import (build_trade_review, coin_from_market, coin_performance, coin_report, format_coin_report, learning_summary, setup_performance, signal_forecast_summary)

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


    def test_coin_report_always_lists_all_four_coins(self):
        class Store:
            def q(self, sql, args=()):
                return [{"series":"KXBTC15M","ticker":"KXBTC15M-X","side":"yes","result":"yes","pnl":2,"stake":10}]
        result = coin_report(Store())
        self.assertEqual({x["coin"] for x in result["coins"]}, {"BTC","BNB","SOL","ETH"})
        self.assertEqual(result["coins"][0]["coin"], "BTC")
        self.assertEqual(next(x for x in result["coins"] if x["coin"]=="SOL")["bets"], 0)

    def test_setup_performance_compares_setup_by_coin_from_saved_snapshot(self):
        class Store:
            def q(self, sql, args=()):
                return [{"review_json": json.dumps({"observed": {
                    "coin":"BTC","result":"yes","side":"yes","pnl":3,"clv":0.02,
                    "signal_snapshot":{"_entry_context":{"chart_setups":[{"name":"range_breakout"}]}}}})},
                    {"review_json": json.dumps({"observed": {
                    "coin":"BTC","result":"no","side":"yes","pnl":-2,"clv":-0.01,
                    "signal_snapshot":{"chart_setups":[{"name":"range_breakout"}]}}})}]
        rows = setup_performance(Store(), min_samples=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["setup"], "range_breakout")
        self.assertEqual(rows[0]["samples"], 2)
        self.assertEqual(rows[0]["wins"], 1)
        self.assertTrue(rows[0]["evidence_level"] == "reviewable")

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


    def test_signal_forecasts_are_scored_against_settled_yes_no_result(self):
        class Store:
            def q(self, sql, args=()):
                return [
                    {"review_json": json.dumps({"observed": {"result":"yes","nearby_signal_readings":[
                        {"signal":"crypto_model","prob":0.9},{"signal":"crypto_model","prob":0.2}]}})},
                    {"review_json": json.dumps({"observed": {"result":"no","nearby_signal_readings":[
                        {"signal":"crypto_model","prob":0.8}]}})},
                ]
        rows = signal_forecast_summary(Store(), min_samples=2)
        self.assertEqual(rows[0]["signal"], "crypto_model")
        self.assertEqual(rows[0]["samples"], 2)
        self.assertEqual(rows[0]["directional_accuracy"], 0.5)
        self.assertAlmostEqual(rows[0]["mean_brier"], (0.01 + 0.64) / 2, places=5)


    def test_coin_specific_poor_setup_history_only_tightens_required_edge(self):
        class Store:
            def q(self, sql, args=()):
                if "trade_reviews" not in sql:
                    return []
                return [{"review_json": json.dumps({"observed": {
                    "result":"no","pnl":-1.0,"clv":-0.02,
                    "signal_snapshot":{"chart_setups":[{"name":"range_breakout"}]}}})}
                    for _ in range(20)]
        result = historical_setup_penalty(Store(), ["range_breakout"], report={},
                                          coin="BTC", min_samples=20)
        self.assertEqual(result["surcharge"], 0.0025)
        self.assertEqual(result["evidence"][0]["source"], "graded_paper_trade_reviews")

    def test_missing_signal_context_is_explicit(self):
        review = build_trade_review({"ticker": "KXETH15M-X", "side": "no", "result": "yes", "pnl": -1})
        self.assertIn("under-documented", " ".join(review["hypotheses_not_proven"]))

if __name__ == "__main__":
    unittest.main()
