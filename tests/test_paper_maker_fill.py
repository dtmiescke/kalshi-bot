"""Paper limit orders fill only up to the size offered through the limit price."""
import unittest

from bot.live.runner import LiveRunner


class Book:
    def __init__(self, no_bids):
        self.no = no_bids

    def as_rest_book(self):
        return {"yes": [], "no": self.no}   # YES asks mirror NO bids


class TestPaperMakerFill(unittest.TestCase):
    plan = {"side": "yes", "limit": 0.45, "contracts": 40}

    def test_touch_is_not_a_fill(self):
        self.assertEqual(LiveRunner.paper_maker_fill(Book([[0.55, 100]]), self.plan), (0, False))  # ask 0.45 = limit

    def test_fill_is_capped_at_size_offered_through_the_limit(self):
        book = Book([[0.57, 3], [0.56, 5], [0.50, 100]])     # asks 0.43 x3, 0.44 x5, 0.50 x100
        self.assertEqual(LiveRunner.paper_maker_fill(book, self.plan), (8, True))

    def test_deep_cross_fills_the_whole_order(self):
        self.assertEqual(LiveRunner.paper_maker_fill(Book([[0.60, 500]]), self.plan), (40, True))


if __name__ == "__main__":
    unittest.main()
