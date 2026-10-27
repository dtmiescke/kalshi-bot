"""Cross-exchange reference-price comparison tests; no network calls."""
import unittest

from bot.live.analytics import SymbolAnalytics


class FakeBook:
    def __init__(self, mid, last_update):
        self._mid = mid
        self.last_update = last_update

    def mid(self):
        return self._mid


class TestCrossExchangePriceComparison(unittest.TestCase):
    def test_fresh_sources_are_reported_and_median_is_used(self):
        now = 1000.0
        a = SymbolAnalytics("BTC/USD", FakeBook(100.0, now))
        a.add_reference("coinbase", now, 101.0)
        a.add_reference("other_free_venue", now, 99.0)

        self.assertEqual(a.fresh_source_prices(now), {
            "coinbase": 101.0, "other_free_venue": 99.0, "kraken": 100.0
        })
        self.assertEqual(a.composite_price(now), 100.0)
        self.assertAlmostEqual(a.source_divergence_bps(now), 200.0)

    def test_stale_and_invalid_prices_are_excluded(self):
        now = 1000.0
        a = SymbolAnalytics("BTC/USD", FakeBook(100.0, now - 20))
        a.add_reference("coinbase", now - 10, 101.0)
        a.add_reference("bad", now, float("nan"))
        self.assertEqual(a.fresh_source_prices(now), {})
        self.assertIsNone(a.composite_price(now))
        self.assertIsNone(a.source_divergence_bps(now))

    def test_consensus_score_reflects_source_agreement(self):
        now = 1000.0
        a = SymbolAnalytics("BTC/USD", FakeBook(100.0, now))
        a.add_reference("coinbase", now, 100.01)
        result = a.source_consensus(now, max_divergence_bps=25)
        self.assertEqual(result["source_count"], 2)
        self.assertAlmostEqual(result["median_price"], 100.01)
        self.assertGreater(result["agreement_score"], 0)
        self.assertLessEqual(result["agreement_score"], 100)

    def test_even_source_consensus_uses_midpoint_median(self):
        now = 1000.0
        a = SymbolAnalytics("BTC/USD", FakeBook(100.0, now))
        a.add_reference("coinbase", now, 102.0)
        result = a.source_consensus(now)
        self.assertEqual(result["median_price"], 101.0)
        self.assertAlmostEqual(result["spread_bps"], (2.0 / 101.0) * 10000)

    def test_consensus_score_is_unavailable_with_one_source(self):
        now = 1000.0
        a = SymbolAnalytics("BNB/USD", FakeBook(100.0, now))
        result = a.source_consensus(now)
        self.assertEqual(result["source_count"], 1)
        self.assertIsNone(result["agreement_score"])

    def test_future_timestamp_is_not_treated_as_fresh(self):
        now = 1000.0
        a = SymbolAnalytics("BTC/USD", FakeBook(100.0, now + 2))
        a.add_reference("coinbase", now, 101.0)
        self.assertEqual(a.fresh_source_prices(now), {"coinbase": 101.0})


if __name__ == "__main__":
    unittest.main()
