"""Regression tests for fail-closed training and champion promotion."""
import unittest
from types import SimpleNamespace
from unittest import mock

from bot import main, ml
from bot.config import Settings


class TestTrainingSafety(unittest.TestCase):
    def test_existing_champion_must_be_evaluated_before_promotion(self):
        self.assertFalse(ml.beats_champion(True, False, None, 0.2))
        self.assertFalse(ml.beats_champion(True, True, None, 0.2))
        self.assertFalse(ml.beats_champion(True, True, 0.25, 0.25))
        self.assertFalse(ml.beats_champion(True, True, 0.20, 0.21))
        self.assertTrue(ml.beats_champion(True, True, 0.25, 0.20))

    def test_first_champion_does_not_require_a_previous_champion(self):
        self.assertTrue(ml.beats_champion(False, True, None, 0.2))
        self.assertFalse(ml.beats_champion(False, False, None, 0.2))

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

    def test_default_settings_pass_validation_in_paper_mode(self):
        settings = Settings()
        settings.mode = "paper"
        settings.validate()

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
