"""Phase 6: the Council of 5 (+ Quant). Members vote on math-approved
candidates, are graded after settlement, and earn power from their record."""
import os
import tempfile
import unittest
from unittest import mock

from bot import council, grader
from bot.config import Settings
from bot.engine import Engine
from bot.signals.base import Reading
from bot.storage import Store

SPORTS = {"ticker": "KXNBAGAME-26OCT24BOSNYK-BOS", "event_ticker": "KXNBAGAME-26OCT24BOSNYK",
          "series": "KXNBAGAME"}
CRYPTO = {"ticker": "KXBTC15M-W-00", "event_ticker": "KXBTC15M-W", "series": "KXBTC15M"}
PLAN = {"side": "yes", "price": 0.50, "edge": 0.06}


def seed(store, member, results, start=1_000_000, brier=0.20, market=0.25):
    """Graded votes, oldest first; results: 1 = right, 0 = wrong."""
    for i, ok in enumerate(results):
        store.x("""INSERT INTO council_votes (ticker, member, ts, series, side, price, vote,
                   confidence, reason, outcome, correct, brier, market_brier, settled_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"T{member}{start + i}", member, start + i, "KXNBAGAME", "yes", 0.5, "back", 0.6, "r",
                 1, ok, brier, market, start + i))


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))
        self.s = Settings()
        self.s.mode = "paper"

    def tearDown(self):
        self.st.db.close()
        self.d.cleanup()


class TestVotes(Base):
    def test_sports_members_vote_from_their_own_signals(self):
        readings = [Reading("book_consensus", 0.58, 0.9), Reading("ai_news", 0.40, 0.5),
                    Reading("schedule_spot", 0.56, 0.6), Reading("trade_flow", 0.52, 0.4),
                    Reading("kalshi_market", 0.50, 0.9)]
        votes = {v["member"]: v for v in council.member_votes(SPORTS, PLAN, readings, 0.57, 0.8)}
        self.assertEqual(set(votes), {"sharp", "scout", "situationist", "market", "stats"})
        self.assertEqual(votes["sharp"]["vote"], "back")
        self.assertEqual(votes["scout"]["vote"], "pass")              # 40% < 50c
        self.assertEqual(votes["market"]["vote"], "pass")             # 52% < 50c + fee
        self.assertAlmostEqual(votes["sharp"]["confidence"], 0.58)
        self.assertIn("58%", votes["sharp"]["reason"])
        self.assertIn("50c", votes["sharp"]["reason"])

    def test_crypto_seats_the_quant_not_the_sports_members(self):
        readings = [Reading("crypto_model", 0.62, 0.75), Reading("spot_flow", 0.55, 0.5)]
        members = {v["member"] for v in council.member_votes(CRYPTO, PLAN, readings, 0.6, 0.7)}
        self.assertEqual(members, {"quant", "market", "stats"})

    def test_no_side_votes_for_no(self):
        votes = council.member_votes(SPORTS, {"side": "no", "price": 0.40},
                                     [Reading("book_consensus", 0.30, 0.9)])
        self.assertEqual(votes[0]["vote"], "back")                    # NO at 70%
        self.assertAlmostEqual(votes[0]["confidence"], 0.70)

    def test_members_without_data_abstain(self):
        self.assertEqual(council.member_votes(SPORTS, PLAN, [Reading("kalshi_market", 0.5, 0.9)]), [])
        self.assertEqual(council.member_votes(SPORTS, {"side": "x", "price": 0.5}, []), [])

    def test_votes_upsert_and_settled_votes_are_kept(self):
        v = [{"member": "sharp", "vote": "back", "confidence": 0.6, "reason": "a"}]
        self.st.record_council_votes("T1", "KXNBAGAME", "yes", 0.5, v, ts=1)
        self.st.record_council_votes("T1", "KXNBAGAME", "yes", 0.52, [dict(v[0], reason="b")], ts=2)
        rows = self.st.q("SELECT * FROM council_votes")
        self.assertEqual((len(rows), rows[0]["reason"], rows[0]["price"]), (1, "b", 0.52))
        self.st.grade_council_votes("T1", 1, settled_ts=3)
        self.st.record_council_votes("T1", "KXNBAGAME", "no", 0.9, [dict(v[0], reason="c")], ts=4)
        self.assertEqual(self.st.q("SELECT reason FROM council_votes")[0]["reason"], "b")


class TestGrading(Base):
    def test_grading_updates_after_settlement(self):
        votes = [{"member": "sharp", "vote": "back", "confidence": 0.7, "reason": "a"},
                 {"member": "scout", "vote": "pass", "confidence": 0.4, "reason": "b"}]
        self.st.record_council_votes("KXNBAGAME-X-BOS", "KXNBAGAME", "yes", 0.5, votes, ts=1)

        class Client:
            def market(self, ticker):
                return {"result": "yes"}
        self.assertEqual(grader.grade_council_votes(self.st, Client(), now=10), 2)
        rows = {r["member"]: r for r in self.st.q("SELECT * FROM council_votes")}
        self.assertEqual((rows["sharp"]["correct"], rows["scout"]["correct"]), (1, 0))
        self.assertAlmostEqual(rows["sharp"]["brier"], 0.09)
        self.assertAlmostEqual(rows["scout"]["brier"], 0.36)
        self.assertAlmostEqual(rows["sharp"]["market_brier"], 0.25)
        table = council.standings(self.st, self.s)
        self.assertEqual((table["sharp"]["graded"], table["sharp"]["right"]), (1, 1))
        self.assertEqual(table["scout"]["streak"], -1)

    def test_no_side_graded_against_no_result(self):
        self.st.record_council_votes("T", "KXNBAGAME", "no", 0.3,
                                     [{"member": "sharp", "vote": "back", "confidence": 0.8,
                                       "reason": ""}])
        self.st.grade_council_votes("T", 0)
        self.assertEqual(self.st.q("SELECT correct FROM council_votes")[0]["correct"], 1)

    def test_unsettled_markets_stay_ungraded(self):
        self.st.record_council_votes("T", "KXNBAGAME", "yes", 0.5,
                                     [{"member": "sharp", "vote": "back", "confidence": 0.6,
                                       "reason": ""}])

        class Client:
            def market(self, ticker):
                return {"result": ""}
        self.assertEqual(grader.grade_council_votes(self.st, Client()), 0)
        self.assertIsNone(self.st.q("SELECT outcome FROM council_votes")[0]["outcome"])


class TestStandings(Base):
    def test_doghouse_two_for_last_nine_and_seated_last(self):
        seed(self.st, "situationist", [1] * 20 + [0, 1, 0, 0, 0, 1, 0, 0, 0])
        seed(self.st, "sharp", [1, 0] * 10)
        table = council.standings(self.st, self.s)
        self.assertTrue(table["situationist"]["doghouse"])
        self.assertEqual(table["situationist"]["last_window"], "2 for 9")
        self.assertFalse(table["sharp"]["doghouse"])
        self.assertEqual(table["situationist"]["seat"], len(council.MEMBERS))
        self.assertEqual(table["sharp"]["seat"], 1)

    def test_members_without_votes_sit_behind_graded_ones(self):
        seed(self.st, "stats", [1, 0, 0, 0])                          # 25% but has a record
        table = council.standings(self.st, self.s)
        self.assertEqual(table["stats"]["seat"], 1)
        self.assertGreater(table["sharp"]["seat"], 1)

    def test_power_is_earned_and_lost(self):
        seed(self.st, "sharp", [1, 0] * 60)                           # 120 votes, beats market
        self.assertEqual(council.standings(self.st, self.s)["sharp"]["stage"], "veto")
        seed(self.st, "stats", [1, 0] * 160, brier=0.20, market=0.25)  # 320 votes, clear margin
        self.assertEqual(council.standings(self.st, self.s)["stats"]["stage"], "full")
        seed(self.st, "market", [1, 0] * 160, brier=0.249, market=0.25)  # beats, thin margin
        self.assertEqual(council.standings(self.st, self.s)["market"]["stage"], "veto")
        seed(self.st, "scout", [1, 0] * 30)                           # too few votes
        self.assertEqual(council.standings(self.st, self.s)["scout"]["stage"], "advisory")
        # demotion: the newest window of sharp's votes is worse than the market
        seed(self.st, "sharp", [1, 0] * 150, start=2_000_000, brier=0.30, market=0.25)
        self.assertEqual(council.standings(self.st, self.s)["sharp"]["stage"], "advisory")

    def test_doghouse_strips_power(self):
        seed(self.st, "sharp", [1, 0] * 60 + [0] * 9)
        row = council.standings(self.st, self.s)["sharp"]
        self.assertTrue(row["doghouse"])
        self.assertEqual(row["stage"], "advisory")

    def test_threshold_validation(self):
        with mock.patch.dict(os.environ, {"COUNCIL_VETO_MIN_VOTES": "500"}):
            with self.assertRaisesRegex(ValueError, "COUNCIL_VETO_MIN_VOTES"):
                Settings().validate()
        with mock.patch.dict(os.environ, {"COUNCIL_DOGHOUSE_MAX_RIGHT": "9"}):
            with self.assertRaisesRegex(ValueError, "COUNCIL_DOGHOUSE_MAX_RIGHT"):
                Settings().validate()


class TestRuling(unittest.TestCase):
    def table(self, **stages):
        return {m: {"title": m, "stage": stages.get(m, "advisory")} for m in council.MEMBERS}

    def votes(self, **v):
        return [{"member": m, "vote": x, "reason": m} for m, x in v.items()]

    def test_advisory_never_binds(self):
        r = council.ruling(self.votes(sharp="pass", stats="pass", market="pass"), self.table())
        self.assertFalse(r["binding"])
        self.assertEqual(r["decision"], "back")

    def test_veto_members_block_only_together(self):
        t = self.table(sharp="veto", stats="veto")
        self.assertEqual(council.ruling(self.votes(sharp="pass", stats="back"), t)["decision"], "back")
        r = council.ruling(self.votes(sharp="pass", stats="pass", market="back"), t)
        self.assertEqual((r["decision"], r["binding"]), ("pass", True))

    def test_full_vote_majority_decides(self):
        t = self.table(sharp="full", stats="veto", market="veto")
        self.assertEqual(council.ruling(self.votes(sharp="back", stats="pass", market="pass"), t)
                         ["decision"], "pass")
        self.assertEqual(council.ruling(self.votes(sharp="back", stats="pass", market="back"), t)
                         ["decision"], "back")


class TestEngineAdvisory(Base):
    """The math makes the call: advisory members never change a bet decision."""

    BOOK = {"yes": [(0.11, 500)], "no": [(0.88, 500)]}               # YES ask $0.12

    def setUp(self):
        super().setUp()
        book = self.BOOK

        class Data:
            def orderbook(self, ticker):
                return book
        self.eng = Engine(self.s, self.st, data_client=Data(), llm=False)

    def _evaluate(self):
        readings = [Reading("kalshi_market", 0.115, 0.9, {"mid": 0.115}),
                    Reading("crypto_model", 0.30, 0.75, {}),
                    Reading("spot_flow", 0.05, 0.6, {})]                # Market Reader says pass
        summary = {"skipped": {}}
        with mock.patch("bot.engine.brain.blend", return_value=(0.30, 0.9)), \
                mock.patch("bot.engine.brain.calibrate", side_effect=lambda p, *a, **k: p):
            got = self.eng.evaluate(dict(CRYPTO), readings, {}, summary, required="crypto_model",
                                    min_edge=0.05, max_per_bet=10)
        return got, summary

    def test_advisory_stage_never_changes_the_decision(self):
        with_council, _ = self._evaluate()
        self.assertTrue(self.st.q("SELECT * FROM council_votes WHERE member='market'"))
        self.st.x("DELETE FROM council_votes")
        with mock.patch("bot.engine.council.convene", return_value={"binding": False}):
            without, _ = self._evaluate()
        self.assertIsNotNone(with_council)
        self.assertEqual(with_council[1], without[1])
        decisions = [r["decision"] for r in self.st.q("SELECT decision FROM decision_audit")]
        self.assertEqual(decisions, ["candidate", "candidate"])

    def test_earned_veto_blocks_the_trade(self):
        seed(self.st, "market", [1, 0] * 60)                          # Market Reader earned a veto
        got, summary = self._evaluate()
        self.assertIsNone(got)
        self.assertEqual(summary["skipped"].get("council veto"), 1)
        reason = self.st.q("SELECT reason FROM decision_audit")[0]["reason"]
        self.assertIn("council veto (Market Reader)", reason)

    def test_council_error_falls_back_to_the_math(self):
        with mock.patch("bot.council.member_votes", side_effect=RuntimeError("boom")):
            got, _ = self._evaluate()
        self.assertIsNotNone(got)


class TestSummary(Base):
    def test_summary_carries_member_standings(self):
        from bot import dashboard
        seed(self.st, "sharp", [1, 1, 0])
        out = dashboard.summary(self.st)
        self.assertEqual(out["members"]["sharp"]["recent"], "2-1")


class TestLLMReviewFallback(Base):
    def setUp(self):
        super().setUp()
        self.summary = {"spot": {"data_quality": {"stale": False, "exchanges_fresh": 2}},
                        "kalshi_book_age_s": 1.0, "signals": {"crypto_model": 0.62}}

    def test_required_mode_uses_numeric_gate_when_budget_is_exhausted(self):
        class ExhaustedLLM:
            def budget_left(self):
                return 0
        with mock.patch.dict(os.environ, {"COUNCIL_MODE": "required"}):
            result = council.review(ExhaustedLLM(), self.st, "KXBTC15M-X", self.summary)
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["by"], "numbers")
        self.assertIn("budget unavailable", result["reason"])

    def test_required_mode_uses_numeric_gate_when_model_returns_no_chair(self):
        class BrokenLLM:
            models = ["model-a", "model-b"]
            dead = set()
            def budget_left(self):
                return 6
            def ask_json(self, prompt, model):
                return None, None
        with mock.patch.dict(os.environ, {"COUNCIL_MODE": "required"}):
            result = council.review(BrokenLLM(), self.st, "KXBTC15M-Y", self.summary)
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["by"], "numbers")
        self.assertIn("chair response unavailable", result["reason"])

    def test_valid_chair_rejection_is_preserved(self):
        class VotingLLM:
            models = ["model-a", "model-b"]
            dead = set()
            def budget_left(self):
                return 6
            def ask_json(self, prompt, model):
                if prompt.startswith("You are the skeptic on a trading council"):
                    return {"severe_issue": False, "issues": []}, model
                return {"chair": {"decision": "reject", "reason": "edge too thin"}}, model
        with mock.patch.dict(os.environ, {"COUNCIL_MODE": "required"}):
            result = council.review(VotingLLM(), self.st, "KXBTC15M-Z", self.summary)
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["by"], "council")
        self.assertIn("edge too thin", result["reason"])


if __name__ == "__main__":
    unittest.main()
