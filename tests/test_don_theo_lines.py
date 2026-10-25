"""Phase 6: Don Theo's lines. Bad news first, moods from real results (incl.
the 5-win Hot hand), AI lines only within the cap, fallback lines, never blank,
and the Sunday Sit-Down."""
import os
import random
import tempfile
import unittest
from unittest import mock

from bot import briefing, don_theo
from bot.config import Settings
from bot.storage import Store

SUNDAY = 1792929600            # 2026-10-25 12:00 UTC, a Sunday
OK = (True, "ok")


class FakeLLM:
    daily_limit = 45

    def __init__(self, reply="Pay the dog, the nose knows."):
        self.reply, self.calls = reply, 0

    def budget_left(self):
        return 5

    def _day_key(self):
        return "llm_calls_test"

    def ask(self, prompt, **kw):
        self.calls += 1
        return self.reply, "m"


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()
        self.s.mode = "paper"

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()

    def bets(self, pnls, end=SUNDAY - 3600, step=3600):
        """Graded paper bets, oldest first."""
        for i, pnl in enumerate(pnls):
            ts = end - (len(pnls) - 1 - i) * step
            self.st.x("""INSERT INTO bets (ts, mode, ticker, side, price, contracts, stake, fee,
                         status, result, pnl, graded_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (ts - 600, "paper", f"KXNBAGAME-{i}", "yes", 0.5, 10, 5.0, 0.2, "graded",
                       "yes" if pnl > 0 else "no", pnl, ts))

    def speak(self, llm=None, betting=OK):
        return don_theo.speak(self.st, self.s, llm, now=SUNDAY, rng=random.Random(1),
                              betting=betting)


class TestBadNewsFirst(Base):
    def test_losing_week_is_said_plainly_before_the_joke(self):
        self.bets([-8.0, -6.0])
        out = self.speak(FakeLLM("Even the best dog misses a squirrel."))
        self.assertTrue(out["line"].startswith("We're down $14.00 this week. That's real."),
                        out["line"])
        self.assertLess(out["line"].index("That's real"), out["line"].index("squirrel"))
        self.assertEqual(out["title"], "Don Theo")

    def test_pause_and_benched_source_come_first(self):
        self.bets([3.0])
        self.st.set_kv("health_paused_areas", {"sports": "health check odds: feed down"})
        out = self.speak(betting=(False, "daily loss limit hit"))
        self.assertTrue(out["line"].startswith("Betting's paused: daily loss limit hit."))
        self.assertIn("Sports is benched: health check odds: feed down.", out["line"])
        self.assertEqual(len(out["bad_news"]), 2)

    def test_no_bad_news_no_prefix(self):
        self.bets([3.0, 2.0])
        out = self.speak()
        self.assertEqual(out["bad_news"], [])
        self.assertEqual(out["title"], "Rott Master")
        self.assertNotIn("That's real", out["line"])

    def test_rule_helper(self):
        self.assertEqual(don_theo.first_bad_news([], "joke"), "joke")
        self.assertTrue(don_theo.first_bad_news(["Down $5."], "joke").startswith("Down $5."))


class TestMoods(Base):
    def test_each_mood(self):
        self.assertEqual(don_theo.mood(12.0, 2), "smooth")
        self.assertEqual(don_theo.mood(0.0, 0), "all_business")
        self.assertEqual(don_theo.mood(0.0, -3), "grumpy")
        self.assertEqual(don_theo.mood(-4.0, 1), "grumpy")
        self.assertEqual(don_theo.mood(-4.0, 5), "hot_hand")
        self.assertEqual(don_theo.mood(10.0, 4), "smooth")

    def test_five_win_hot_hand_from_real_bets(self):
        self.bets([-2.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        out = self.speak()
        self.assertEqual((out["mood"], out["streak"]), ("hot_hand", 5))
        self.assertIn("Somebody get me a bigger chair.", out["line"])
        self.assertIn("Five straight", out["line"])

    def test_losing_streak_is_grumpy(self):
        self.bets([5.0, -1.0, -1.0, -1.0])
        out = self.speak()
        self.assertEqual((out["mood"], out["streak"]), ("grumpy", -3))

    def test_reason_carries_real_numbers(self):
        self.bets([4.0, -1.5])
        self.assertEqual(self.speak()["reason"], "1-1 this week, +$2.50, streak -1")


class TestLines(Base):
    def test_ai_line_used_within_cap(self):
        llm = FakeLLM()
        out = self.speak(llm)
        self.assertEqual((out["by"], llm.calls), ("ai", 1))
        self.assertIn("Pay the dog", out["line"])

    def test_fallback_when_ai_cap_hit(self):
        llm = FakeLLM()
        with mock.patch.dict(os.environ, {"DON_THEO_AI_PER_DAY": "1"}):
            self.speak(llm)
            out = self.speak(llm)
        self.assertEqual((out["by"], llm.calls), ("line_bank", 1))
        self.assertTrue(out["line"].strip())

    def test_fallback_keeps_trading_reserve(self):
        llm = FakeLLM()
        self.st.set_kv("llm_calls_test", llm.daily_limit - briefing.reserve())
        self.assertEqual(self.speak(llm)["by"], "line_bank")
        self.assertEqual(llm.calls, 0)

    def test_failed_or_bad_ai_reply_falls_back(self):
        for reply in (None, "", "   ", "x" * 500):
            out = self.speak(FakeLLM(reply))
            self.assertEqual(out["by"], "line_bank", reply)
            self.assertTrue(out["line"].strip())

    def test_never_blank(self):
        for key, lines in don_theo.LINES.items():
            self.assertTrue(lines and all(l.strip() for l in lines), key)
        out = self.speak()                                    # no bets at all
        self.assertTrue(out["line"].strip())
        self.assertIn(out["line"], don_theo.LINES["opening"])
        self.assertEqual(self.st.get_kv("don_theo_last")["line"], out["line"])

    def test_best_day(self):
        self.bets([2.0, 1.0], end=SUNDAY - 3 * 86400, step=3600)
        self.bets([9.0])
        self.assertIn(self.speak()["line"], don_theo.LINES["best_day"])

    def test_doghouse_line_and_titles(self):
        self.assertEqual(don_theo.doghouse_line("situationist", (2, 9)),
                         "Professor, you're 2 for your last 9. You're on thin ice, pal.")
        self.assertEqual((don_theo.title(False), don_theo.title(True)), ("Rott Master", "Don Theo"))


class TestSundaySitDown(Base):
    def seed_votes(self, member, results, start):
        for i, ok in enumerate(results):
            self.st.x("""INSERT INTO council_votes (ticker, member, ts, series, side, price, vote,
                         confidence, reason, outcome, correct, brier, market_brier, settled_ts)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (f"T{member}{start + i}", member, start + i, "KXNBAGAME", "yes", 0.5,
                       "back", 0.6, "r", 1, ok, 0.2, 0.25, start + i))

    def test_only_on_sunday_once_per_week(self):
        self.assertIsNone(briefing.sunday_sitdown(self.st, self.s, now=SUNDAY - 86400))
        self.assertIsNotNone(briefing.sunday_sitdown(self.st, self.s, now=SUNDAY))
        self.assertIsNone(briefing.sunday_sitdown(self.st, self.s, now=SUNDAY + 3600))
        self.assertIsNotNone(briefing.sunday_sitdown(self.st, self.s, now=SUNDAY + 7 * 86400))

    def test_losing_week_recap_bad_news_first(self):
        self.bets([6.0, -9.0, -11.0])
        self.seed_votes("sharp", [1] * 10, 1_000)
        self.seed_votes("market", [0] * 9, 2_000)
        out = briefing.sunday_sitdown(self.st, self.s, now=SUNDAY)
        text = out["text"]
        self.assertTrue(text.startswith("We're down $14.00 this week. That's real."), text)
        self.assertIn("1-2 this week, -$14.00", text)
        self.assertIn("Best call: KXNBAGAME-0 YES at 50c, +6.00.", text)
        self.assertIn("Worst call: KXNBAGAME-2 YES at 50c, -11.00.", text)
        self.assertIn("Numbers carried us", text)
        self.assertIn("Ticker, you're 0 for your last 9.", text)
        self.assertTrue(text.endswith(don_theo.SIGNATURE))

    def test_better_seat_next_week(self):
        self.seed_votes("sharp", [1] * 10, 1_000)
        self.seed_votes("stats", [1, 0] * 5, 2_000)
        briefing.sunday_sitdown(self.st, self.s, now=SUNDAY)
        self.seed_votes("sharp", [0] * 6, 3_000)               # sharp cools off,
        self.seed_votes("stats", [1] * 20, 4_000)              # stats overtakes
        out = briefing.sunday_sitdown(self.st, self.s, now=SUNDAY + 7 * 86400)
        self.assertIn("Earned a better seat: Calculator.", out["text"])


if __name__ == "__main__":
    unittest.main()
