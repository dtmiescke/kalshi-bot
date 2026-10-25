"""Known problem 4: training stops cleanly at a time limit instead of being killed."""
import os
import random
import tempfile
import time
import unittest

from bot import features, ml
from bot.main import training_due
from bot.storage import Store
from tests.test_advanced import walk


def dataset(store, windows=320):
    rnd, c = random.Random(5), walk(windows * 15 + 420, seed=9)
    for n, w in enumerate(range(400, len(c) - 16, 15)):
        if n >= windows:
            break
        strike = c[w]["close"]
        y = int(c[w + 15]["close"] > strike)
        i = w + 6
        f0 = features.build(c[i - 300:i + 1], strike, 540)
        mid = min(max(0.5 + 0.6 * (f0["model_prob"] - 0.5) + rnd.gauss(0, .04), .03), .97)
        f = features.build(c[i - 300:i + 1], strike, 540, mid - .01, mid + .01)
        ml.add_row(store, f"KXBTC15M-S{w}", c[i]["ts"], "BTC", f, y, "test")


class TestTrainerTimeLimit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.old_dir = ml.MODEL_DIR
        ml.MODEL_DIR = os.path.join(self.d.name, "models")
        self.st = Store(os.path.join(self.d.name, "t.db"))

    def tearDown(self):
        ml.MODEL_DIR = self.old_dir
        self.st.db.close()
        self.d.cleanup()

    def test_past_deadline_stops_without_promoting(self):
        dataset(self.st)
        m = ml.train(self.st, min_windows=300, deadline_ts=time.time() - 1)
        self.assertEqual(m["status"], "time limit")
        self.assertFalse(m["promoted"])
        self.assertIn("model search", m["stage"])
        self.assertFalse(os.path.exists(os.path.join(ml.MODEL_DIR, "crypto_model.joblib")))
        self.assertIn("time limit", ml.text(m))

    def test_no_deadline_behaves_as_before(self):
        self.assertEqual(ml.train(self.st, min_windows=300)["status"], "not enough data")

    def test_time_limit_is_retried_on_the_short_backoff(self):
        now = 1_800_000_000
        self.st.set_kv("last_train_attempt", now - 7 * 3600)
        self.st.set_kv("last_train", now - 3600)          # a recent real training run
        self.st.set_kv("last_train_status", "time limit")
        self.assertTrue(training_due(self.st, now, every_hours=24, retry_hours=6))
        self.st.set_kv("last_train_status", "trained")
        self.assertFalse(training_due(self.st, now, every_hours=24, retry_hours=6))


if __name__ == "__main__":
    unittest.main()
