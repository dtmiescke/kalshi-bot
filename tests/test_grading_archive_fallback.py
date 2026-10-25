"""T2b: settled bets are graded even after Kalshi moves the market to its archive."""
import os
import tempfile
import time
import unittest

from bot import grader, kalshi_history as kh
from bot.storage import Store


class ArchivingClient:
    """Like KalshiClient: market() for live data, _req for everything else.
    Archived tickers 404 on the live endpoint."""

    def __init__(self, live=None, archive=None):
        self.live, self.archive, self.paths = live or {}, archive or {}, []

    def market(self, ticker):
        if ticker not in self.live:
            raise RuntimeError("404 Not Found")
        return self.live[ticker]

    def _req(self, method, path, params=None):
        self.paths.append(path)
        if path.startswith("/historical/markets/"):
            t = path.rsplit("/", 1)[1]
            if t not in self.archive:
                raise RuntimeError("404 Not Found")
            return {"market": self.archive[t]}
        raise RuntimeError("unexpected " + path)

    def _paged(self, *a, **k):
        return []


class TestGradingClient(unittest.TestCase):
    def test_live_first_then_archive_then_missing(self):
        c = ArchivingClient(live={"LIVE": {"result": "yes"}}, archive={"OLD": {"result": "no"}})
        g = kh.GradingClient(c)
        self.assertEqual(g.market("LIVE")["result"], "yes")
        self.assertEqual(c.paths, [])                                   # no archive call needed
        self.assertEqual(g.market("OLD")["result"], "no")
        self.assertEqual(c.paths, ["/historical/markets/OLD"])
        with self.assertRaises(LookupError):
            g.market("NOWHERE")
        self.assertEqual(g.health()["archive_fallbacks"], 1)

    def test_unsettled_live_market_does_not_hit_archive(self):
        c = ArchivingClient(live={"OPEN": {"result": ""}})
        self.assertEqual(kh.GradingClient(c).market("OPEN")["result"], "")
        self.assertEqual(c.paths, [])

    def test_client_without_archive_access_still_works(self):
        class LiveOnly:
            def market(self, t):
                raise RuntimeError("404")
        with self.assertRaises(LookupError):
            kh.GradingClient(LiveOnly()).market("X")

    def test_settlement_reports_index_value_not_payout(self):
        c = ArchivingClient(archive={"KXBTC15M-W-00": {
            "result": "yes", "expiration_value": "100123.45", "settlement_value_dollars": "1.0000"}})
        result, value, source = kh.HistoryRouter(c).settlement("KXBTC15M-W-00")
        self.assertEqual((result, value, source), ("yes", "100123.45", "archive"))


class TestGradeUsesArchive(unittest.TestCase):
    def test_archived_bet_is_graded(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            st.add_bet({"ts": int(time.time()) - 86400 * 40, "mode": "paper", "ticker": "KXNBAGAME-X-BOS",
                        "event_ticker": "KXNBAGAME-X", "series": "KXNBAGAME", "team": "BOS",
                        "side": "yes", "price": 0.4, "contracts": 10, "stake": 4.0, "fee": 0.17,
                        "model_prob": 0.5, "edge": 0.05, "confidence": 0.8, "signals": {}})
            c = ArchivingClient(archive={"KXNBAGAME-X-BOS": {"result": "yes"}})
            out = grader.grade(st, c)
            self.assertEqual(out["bets"], 1)
            bet = st.q("SELECT status, result, pnl FROM bets")[0]
            self.assertEqual((bet["status"], bet["result"]), ("graded", "yes"))
            self.assertAlmostEqual(bet["pnl"], round(10 * 0.6 - 0.17, 2))
            health = st.get_kv("grading_lookup_health")
            self.assertEqual(health["archive_fallbacks"], 1)
            st.db.close()


if __name__ == "__main__":
    unittest.main()
