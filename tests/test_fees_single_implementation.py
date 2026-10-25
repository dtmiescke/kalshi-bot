"""T2a: one fee implementation, with each series' Kalshi fee type and multiplier."""
import os
import tempfile
import unittest

from bot import fees, risk
from bot.config import Settings
from bot.storage import Store


class TestOneFormula(unittest.TestCase):
    def test_risk_taker_fee_is_fees_formula(self):
        for contracts in (1, 3, 10, 37, 100):
            for cents in range(1, 100):
                p = cents / 100
                for rate in (0.07, 0.0175, 0.035):
                    self.assertEqual(risk.taker_fee(contracts, p, rate),
                                     fees.fee_at_rate(contracts, p, rate), (contracts, p, rate))

    def test_rounds_up_to_the_cent(self):
        self.assertEqual(fees.fee_at_rate(10, 0.09, 0.07), 0.06)     # 0.0573 -> 0.06
        self.assertEqual(fees.fee_at_rate(100, 0.50, 0.07), 1.75)    # exact stays exact
        self.assertEqual(fees.fee_at_rate(5, 0.50, 0.0), 0.0)


class TestSeriesRates(unittest.TestCase):
    def setUp(self):
        fees._live.clear()
        self.s = Settings()

    def test_crypto_series_have_no_maker_fees(self):
        for series in ("KXBTC15M", "KXETH15M-26OCT250130-30", "KXBTCD"):
            self.assertEqual(fees.rate(series, self.s), 0.07)
            self.assertEqual(fees.rate(series, self.s, maker=True), 0.0)

    def test_sports_maker_fees_and_mlb_half_multiplier(self):
        self.assertEqual(fees.rate("KXNBAGAME-X-BOS", self.s, maker=True), 0.0175)
        self.assertAlmostEqual(fees.rate("KXMLBGAME", self.s), 0.035)
        self.assertAlmostEqual(fees.rate("KXMLBGAME", self.s, maker=True), 0.00875)

    def test_unknown_or_flat_series_are_conservative(self):
        self.assertEqual(fees.rate("KXUNKNOWN", self.s), self.s.taker_fee_rate)
        self.assertEqual(fees.rate("KXUNKNOWN", self.s, maker=True), self.s.maker_fee_rate)
        fees._live["KXFLAT"] = ("flat", 1.0)
        self.assertEqual(fees.rate("KXFLAT", self.s, maker=True), self.s.maker_fee_rate)
        self.assertEqual(fees.rate(None, self.s), self.s.taker_fee_rate)

    def test_combo_maker_share_is_half(self):
        fees._live["KXCOMBO"] = ("quadratic_with_combo_maker_fees", 1.0)
        self.assertAlmostEqual(fees.rate("KXCOMBO", self.s, maker=True), 0.035)

    def test_decide_uses_series_rates(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            book = {"yes": [(0.48, 200)], "no": [(0.48, 200)]}       # YES bid .48, ask .52
            crypto = {"ticker": "KXBTC15M-W-00", "event_ticker": "KXBTC15M-W", "series": "KXBTC15M"}
            plan, why = risk.decide(self.s, st, crypto, 0.65, 0.9, book, min_edge=0.03, maker=True)
            self.assertEqual((plan["style"], plan["fee"]), ("maker", 0.0), why)
            mlb = {"ticker": "KXMLBGAME-X-NYY", "event_ticker": "KXMLBGAME-X", "series": "KXMLBGAME"}
            plan, _ = risk.decide(self.s, st, mlb, 0.65, 0.9, book, min_edge=0.03)
            self.assertEqual(plan["fee"], fees.fee_at_rate(plan["contracts"], plan["price"], 0.035))
            st.db.close()


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _req(self, method, path):
        self.calls.append(path)
        series = path.rsplit("/", 1)[-1]
        a = self.answers.get(series)
        if isinstance(a, Exception):
            raise a
        return {"series": a or {}}


class TestRefresh(unittest.TestCase):
    def setUp(self):
        fees._live.clear()
        self.d = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        fees._live.clear()
        self.st.db.close()
        self.d.cleanup()

    def test_refresh_updates_rates_and_reports_changes(self):
        client = FakeClient({"KXBTC15M": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}})
        self.assertEqual(fees.refresh(self.st, client, ["KXBTC15M"], now=1000), {})
        self.assertEqual(fees.rate("KXBTC15M", maker=True), 0.0175)     # Kalshi added maker fees
        client.answers["KXBTC15M"] = {"fee_type": "quadratic", "fee_multiplier": 2}
        changed = fees.refresh(self.st, client, ["KXBTC15M"], now=1000 + 25 * 3600)
        self.assertEqual(changed["KXBTC15M"]["to"], ["quadratic", 2.0])
        self.assertEqual(fees.rate("KXBTC15M"), 0.14)

    def test_refresh_is_daily_and_keeps_old_values_on_bad_answers(self):
        client = FakeClient({"KXNBAGAME": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}})
        fees.refresh(self.st, client, ["KXNBAGAME-X-BOS"], now=0)
        fees.refresh(self.st, client, ["KXNBAGAME"], now=3600)            # not due
        self.assertEqual(client.calls, ["/series/KXNBAGAME"])
        client.answers["KXNBAGAME"] = {"fee_type": "mystery", "fee_multiplier": 1}
        fees.refresh(self.st, client, ["KXNBAGAME"], now=30 * 3600)
        client.answers["KXNBAGAME"] = RuntimeError("down")
        fees.refresh(self.st, client, ["KXNBAGAME"], now=60 * 3600)
        self.assertEqual(fees.lookup("KXNBAGAME"), ("quadratic_with_maker_fees", 1.0))

    def test_load_restores_saved_table(self):
        self.st.set_kv("series_fees", {"KXNEW": {"fee_type": "quadratic", "multiplier": 0.5, "ts": 1},
                                       "KXBAD": {"fee_type": "quadratic", "multiplier": "nan"}})
        fees.load(self.st)
        self.assertEqual(fees.lookup("KXNEW"), ("quadratic", 0.5))
        self.assertIsNone(fees.lookup("KXBAD"))


if __name__ == "__main__":
    unittest.main()
