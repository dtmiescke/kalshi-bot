"""Regression tests for fail-closed training and champion promotion."""
import unittest
from types import SimpleNamespace
from unittest import mock

from bot import engine, guard, main, ml
from bot.config import Settings


class TestTrainingSafety(unittest.TestCase):
    def test_insufficient_training_data_retries_after_short_backoff(self):
        state = {"last_train": 9_900.0, "last_train_attempt": 10_000.0,
                 "last_train_status": "not enough data"}
        store = mock.Mock()
        store.get_kv.side_effect = lambda key, default=0: state.get(key, default)

        self.assertFalse(main.training_due(store, 10_000.0 + 5 * 3600,
                                           every_hours=24, retry_hours=6))
        self.assertTrue(main.training_due(store, 10_000.0 + 6 * 3600,
                                          every_hours=24, retry_hours=6))

    def test_successful_training_keeps_daily_cadence(self):
        state = {"last_train": 10_000.0, "last_train_attempt": 10_000.0,
                 "last_train_status": "trained"}
        store = mock.Mock()
        store.get_kv.side_effect = lambda key, default=0: state.get(key, default)

        self.assertFalse(main.training_due(store, 10_000.0 + 12 * 3600,
                                           every_hours=24, retry_hours=6))
        self.assertTrue(main.training_due(store, 10_000.0 + 24 * 3600,
                                          every_hours=24, retry_hours=6))

    def test_missing_training_status_triggers_one_migration_attempt(self):
        state = {"last_train": 10_000.0, "last_train_attempt": 0}
        store = mock.Mock()
        store.get_kv.side_effect = lambda key, default=0: state.get(key, default)
        self.assertTrue(main.training_due(store, 100_000.0,
                                          every_hours=24, retry_hours=6))

    def test_promotion_holdout_excludes_windows_that_started_before_champion_training(self):
        groups = ["old", "old", "crossing", "crossing", "fresh", "fresh"]
        stamps = [10, 11, 19, 21, 22, 23]
        mask = ml.fresh_test_mask(groups, stamps, champion_trained_at=20)
        self.assertEqual(mask.tolist(), [False, False, False, False, True, True])

    def test_existing_champion_must_be_evaluated_before_promotion(self):
        self.assertFalse(ml.beats_champion(True, False, None, 0.2))
        self.assertFalse(ml.beats_champion(True, True, None, 0.2))
        self.assertFalse(ml.beats_champion(True, True, 0.25, 0.25))
        self.assertFalse(ml.beats_champion(True, True, 0.20, 0.21))
        self.assertTrue(ml.beats_champion(True, True, 0.25, 0.20))

    def test_first_champion_does_not_require_a_previous_champion(self):
        self.assertTrue(ml.beats_champion(False, True, None, 0.2))
        self.assertFalse(ml.beats_champion(False, False, None, 0.2))

    def test_invalid_drawdown_circuit_breaker_setting_fails_closed(self):
        for value in ("nan", "inf", "-1", "101", "not-a-number"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ", {"MAX_DRAWDOWN": value}
            ):
                store = mock.Mock()
                self.assertTrue(guard.drawdown_tripped(store, 1000.0, mode="live"))
                store.q.assert_not_called()

    def test_daily_loss_setting_does_not_create_a_separate_stop(self):
        for value in ("nan", "inf", "-1", "not-a-number"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ", {"DAILY_LOSS_LIMIT": value}
            ):
                store = mock.Mock()
                store.get_kv.return_value = None
                store.q.return_value = [{"p": 0}]
                allowed, reason = guard.status(store, 1000.0, mode="live")
                self.assertTrue(allowed, reason)

    def test_settings_reject_nonfinite_or_unsafe_risk_values(self):
        for field, value in (
            ("bankroll", float("nan")),
            ("max_per_bet", float("inf")),
            ("kelly_fraction", 1.5),
            ("min_edge", 0.0),
            ("taker_fee_rate", 0.0),
            ("order_style", "unknown"),
        ):
            with self.subTest(field=field):
                settings = Settings()
                setattr(settings, field, value)
                with self.assertRaises((ValueError, RuntimeError)):
                    settings.validate()

    def test_scheduled_path_validates_risk_settings_before_other_work(self):
        settings = Settings()
        settings.mode = "paper"
        settings.max_per_bet = float("nan")
        with self.assertRaisesRegex(ValueError, "MAX_PER_BET"):
            main.run_scheduled(settings, mock.Mock(), mock.Mock())

    def test_default_settings_pass_validation_in_paper_mode(self):
        settings = Settings()
        settings.mode = "paper"
        settings.validate()

    def test_invalid_odds_max_age_setting_is_rejected(self):
        for value in ("nan", "inf", "0", "-1"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ", {"ODDS_MAX_AGE_MINUTES": value}
            ):
                with self.assertRaisesRegex(ValueError, "ODDS_MAX_AGE_MINUTES"):
                    Settings(mode="paper").validate()
        with mock.patch.dict("os.environ", {"ODDS_MAX_AGE_MINUTES": "not-a-number"}):
            with self.assertRaises(ValueError):
                Settings(mode="paper")

    def test_invalid_runtime_intervals_are_rejected(self):
        for field, value, message in (
            ("odds_refresh_minutes", 0, "ODDS_REFRESH_MINUTES"),
            ("odds_refresh_minutes", -1, "ODDS_REFRESH_MINUTES"),
            ("maker_wait_s", 0, "MAKER_WAIT_SECONDS"),
            ("maker_wait_s", -1, "MAKER_WAIT_SECONDS"),
        ):
            with self.subTest(field=field, value=value):
                settings = Settings(mode="paper")
                setattr(settings, field, value)
                with self.assertRaisesRegex(ValueError, message):
                    settings.validate()

    def test_execution_boundary_blocks_new_order_when_circuit_breaker_trips(self):
        obj = engine.Engine.__new__(engine.Engine)
        obj.s = SimpleNamespace(mode="live", bankroll=1000.0, taker_fee_rate=0.07)
        obj.store = mock.Mock()
        obj.trade = mock.Mock()
        summary = {"bets": 0, "skipped": {}, "alerts": []}
        snap = {"ticker": "T", "event_ticker": "E", "series": "KXNBAGAME", "team": "X"}
        plan = {"side": "yes", "limit": 0.50, "price": 0.50, "contracts": 1,
                "stake": 0.50, "fee": 0.02, "edge": 0.10}
        with mock.patch("bot.engine.guard.status", return_value=(False, "daily loss limit hit")):
            obj.place(snap, plan, [], 0.60, 0.80, summary, 123)
        obj.trade.place_limit_buy.assert_not_called()
        obj.store.add_bet.assert_not_called()
        self.assertEqual(summary["bets"], 0)
        self.assertTrue(summary["skipped"])

    def test_final_execution_recheck_blocks_when_current_risk_budget_is_gone(self):
        obj = engine.Engine.__new__(engine.Engine)
        obj.s = SimpleNamespace(
            mode="live", bankroll=1000.0, min_edge=0.03, crypto_min_edge=0.05,
            max_per_bet=25.0, crypto_max_per_bet=10.0, taker_fee_rate=0.07
        )
        obj.store = mock.Mock()
        obj.data = SimpleNamespace(orderbook=mock.Mock(return_value={"yes": [], "no": []}))
        obj.trade = mock.Mock()
        summary = {"bets": 0, "skipped": {}, "alerts": []}
        snap = {"ticker": "T", "event_ticker": "E", "series": "KXNBAGAME", "team": "X"}
        plan = {"side": "yes", "price": 0.50, "limit": 0.50, "contracts": 1,
                "stake": 0.50, "fee": 0.02, "edge": 0.10}
        with mock.patch("bot.engine.guard.status", return_value=(True, "ok")), \
             mock.patch("bot.engine.risk.decide", return_value=(None, "risk caps reached")):
            obj.place(snap, plan, [], 0.60, 0.80, summary, 123)
        obj.trade.place_limit_buy.assert_not_called()
        obj.store.add_bet.assert_not_called()
        self.assertTrue(summary["skipped"])

    def test_partial_maker_fill_records_only_filled_stake_and_fee(self):
        saved = []
        obj = engine.Engine.__new__(engine.Engine)
        obj.s = SimpleNamespace(mode="demo", taker_fee_rate=0.07, maker_fee_rate=0.0175)
        obj.store = SimpleNamespace(add_bet=lambda row: saved.append(row))
        # A series that charges maker fees, so the fee on the filled part is visible.
        snap = {"ticker": "T", "event_ticker": "E", "series": "KXNBAGAME", "team": "BOS"}
        plan = {"side": "yes", "price": 0.50, "limit": 0.50, "style": "maker",
                "contracts": 5, "stake": 2.50, "fee": 0.05, "edge": 0.10}
        with mock.patch("bot.engine.alerts.send"):
            obj.place(snap, plan, [], 0.60, 0.80, {"bets": 0}, 123,
                      executed={"order_id": "ORDER", "contracts": 2})
        self.assertEqual(saved[0]["contracts"], 2)
        self.assertEqual(saved[0]["stake"], 1.00)
        self.assertEqual(saved[0]["fee"], 0.01)
        # Kalshi's crypto series are fee_type "quadratic": resting fills are free.
        saved.clear()
        snap = {"ticker": "T", "event_ticker": "E", "series": "KXBTC15M", "team": "BTC"}
        with mock.patch("bot.engine.alerts.send"):
            obj.place(snap, dict(plan), [], 0.60, 0.80, {"bets": 0}, 123,
                      executed={"order_id": "ORDER", "contracts": 2})
        self.assertEqual(saved[0]["fee"], 0.0)

    def test_sports_arbitrage_fails_closed_for_unverified_or_draw_capable_series(self):
        book_a = {"yes": [(0.40, 10)], "no": [(0.65, 10)]}
        book_b = {"yes": [(0.50, 10)], "no": [(0.60, 10)]}
        self.assertIsNone(guard.arb_plan(
            "KXSOCCERGAME", book_a, book_b, 0.07, 20.0
        ))
        self.assertIsNone(guard.arb_plan(
            "KXNFLGAME", book_a, book_b, 0.07, 20.0
        ))
        self.assertIsNotNone(guard.arb_plan(
            "KXNBAGAME", book_a, book_b, 0.07, 20.0
        ))

    def test_arbitrage_budget_respects_open_position_and_cash_caps(self):
        obj = engine.Engine.__new__(engine.Engine)
        obj.s = SimpleNamespace(
            mode="paper", bankroll=1000.0, max_per_day=100.0, max_per_game=50.0,
            max_per_bet=25.0, max_open_cost=200.0, max_open_positions=5,
            taker_fee_rate=0.07
        )
        obj.store = mock.Mock()
        obj.store.staked_since.return_value = 0.0
        obj.store.staked_on_event.return_value = 0.0
        obj.store.account.return_value = {
            "open_positions": 4, "open_cost": 0.0, "cash": 1000.0
        }
        self.assertEqual(obj._arbitrage_budget("EV"), 0.0)
        obj.store.account.return_value = {
            "open_positions": 0, "open_cost": 190.0, "cash": 20.0
        }
        budget = obj._arbitrage_budget("EV")
        self.assertGreater(budget, 0.0)
        self.assertLessEqual(budget, 9.95 / 1.07)

    def test_incomplete_arbitrage_unwind_keeps_residual_position_in_ledger(self):
        saved = []
        obj = engine.Engine.__new__(engine.Engine)
        obj.s = SimpleNamespace(mode="live", taker_fee_rate=0.07)
        obj.store = SimpleNamespace(add_bet=lambda row: saved.append(row))
        obj.data = SimpleNamespace(orderbook=lambda ticker: {"yes": [(0.25, 10)], "no": []})
        obj.trade = SimpleNamespace(place_limit_sell=lambda *args: {"fill_count": 0})
        with mock.patch("bot.engine.alerts.send"):
            residual = obj._unwind_leg("T", "yes", 0.30, "BUY-1", 2,
                                        "EVENT", "KXNBAGAME", team="Team")
        self.assertEqual(residual, 2)
        self.assertEqual(saved[0]["contracts"], 2)
        self.assertEqual(saved[0]["event_ticker"], "EVENT")
        self.assertTrue(saved[0]["signals"]["arbitrage"]["unwind_residual"])

    def test_partial_two_leg_arb_fill_unwinds_only_filled_contracts(self):
        for method in ("try_arb", "execute_ladder"):
            with self.subTest(method=method):
                obj = engine.Engine.__new__(engine.Engine)
                obj.s = SimpleNamespace(
                    mode="live", bankroll=1000.0, max_per_day=100.0, max_per_game=100.0,
                    max_per_bet=25.0, max_open_cost=200.0, max_open_positions=5,
                    taker_fee_rate=0.07
                )
                obj.store = mock.Mock()
                obj.store.has_open_event_bet.return_value = False
                obj.store.staked_since.return_value = 0.0
                obj.store.staked_on_event.return_value = 0.0
                obj.store.account.return_value = {
                    "open_positions": 0, "open_cost": 0.0, "cash": 1000.0
                }
                low_book = {"yes": [(0.25, 100)], "no": [(0.70, 2)]}
                high_book = {"yes": [(0.60, 2)], "no": [(0.55, 100)]}
                obj.data = SimpleNamespace(orderbook=lambda ticker: (
                    low_book if ticker in ("LOW", "A") else high_book
                ))
                calls = {"buy": 0, "sells": []}

                def buy(ticker, side, qty, price):
                    calls["buy"] += 1
                    return {"order_id": "O1", "fill_count": 1}

                def sell(ticker, side, qty, price):
                    calls["sells"].append((ticker, side, qty, price))
                    return {"fill_count": qty}

                obj.trade = SimpleNamespace(place_limit_buy=buy, place_limit_sell=sell)
                summary = {"bets": 0, "arbs": []}
                with mock.patch("bot.engine.alerts.send"), mock.patch(
                    "bot.engine.guard.status", return_value=(True, "ok")
                ):
                    if method == "try_arb":
                        result = obj.try_arb(
                            {"event_ticker": "EV", "series_ticker": "KXNBAGAME"},
                            [{"ticker": "A", "yes_sub_title": "A"},
                             {"ticker": "B", "yes_sub_title": "B"}],
                            summary, 123
                        )
                        self.assertFalse(result)
                    else:
                        result = obj.execute_ladder(
                            {"low": "LOW", "high": "HIGH"}, 123, summary
                        )
                        self.assertIsNone(result)
                self.assertEqual(calls["buy"], 1)
                self.assertEqual(calls["sells"], [("A" if method == "try_arb" else "LOW",
                                                   "yes", 1, 0.25)])
                # The sell response lacks an order ID, so the unwind is unconfirmed.
                # The remaining exposure must be recorded, not hidden from risk checks.
                obj.store.add_bet.assert_called_once()
                residual = obj.store.add_bet.call_args.args[0]
                self.assertEqual(residual["ticker"], "A" if method == "try_arb" else "LOW")
                self.assertEqual(residual["contracts"], 1)
                self.assertTrue(residual["signals"]["arbitrage"]["unwind_residual"])
                self.assertEqual(residual["signals"]["arbitrage"]["unwound"], 0)

    def test_train_command_exits_nonzero_when_data_is_insufficient(self):
        settings = SimpleNamespace(
            db_path="unused.sqlite",
            market_data_url=lambda: "https://example.invalid",
            data_key=lambda: (None, None),
            taker_fee_rate=0.07,
        )
        store = mock.Mock()
        with (
            mock.patch.object(main, "Settings", return_value=settings),
            mock.patch.object(main, "Store", return_value=store),
            mock.patch.object(main, "KalshiClient"),
            mock.patch.object(ml, "train", return_value={
                "status": "not enough data", "windows": 0, "needed": 300
            }),
            mock.patch.object(ml, "text", return_value="training skipped"),
        ):
            with self.assertRaises(SystemExit) as exc:
                main.main(["train"])
        self.assertEqual(exc.exception.code, 2)
        store.set_kv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
