"""Known problem 5: the WebSocket book reads Kalshi's current fixed-point fields."""
import unittest

from bot.live import feeds
from bot.live.kalshi_book import KalshiBook


class TestWebSocketBookFixedPoint(unittest.TestCase):
    def test_snapshot_in_documented_fp_shape_is_not_empty(self):
        # shape per docs.kalshi.com/websockets/orderbook-updates
        books = {"KXBTC15M-W-00": KalshiBook("KXBTC15M-W-00")}
        feeds.handle_kalshi({"type": "orderbook_snapshot", "sid": 2, "seq": 1, "msg": {
            "market_ticker": "KXBTC15M-W-00",
            "yes_dollars_fp": [["0.4400", "120.00"], ["0.4300", "35.50"]],
            "no_dollars_fp": [["0.5400", "80.00"]]}}, books)
        b = books["KXBTC15M-W-00"]
        self.assertEqual(b.yes, {0.44: 120.0, 0.43: 35.5})
        self.assertEqual(b.no, {0.54: 80.0})
        self.assertAlmostEqual(b.mid(), (0.44 + 0.46) / 2)

        # deltas in the documented shape apply on top
        feeds.handle_kalshi({"type": "orderbook_delta", "seq": 2, "msg": {
            "market_ticker": "KXBTC15M-W-00", "price_dollars": "0.4400",
            "delta_fp": "-20.00", "side": "yes"}}, books)
        self.assertEqual(b.yes[0.44], 100.0)

    def test_absent_side_means_empty_and_legacy_shapes_still_parse(self):
        b = KalshiBook("T")
        b.snapshot({"yes_dollars_fp": [["0.9990", "473020.62"]]}, seq=1)
        self.assertEqual(b.yes, {0.999: 473020.62})
        self.assertEqual(b.no, {})
        b.snapshot({"yes_dollars": [["0.55", "10"]], "no": [[41, 80]]}, seq=2)
        self.assertEqual((b.yes, b.no), ({0.55: 10.0}, {0.41: 80.0}))


if __name__ == "__main__":
    unittest.main()
