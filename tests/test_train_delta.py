"""No-pause training (bot/train_delta.py): the trainer exports only what it added;
the bot imports it into its newer memory once, atomically, without touching
anything the bot wrote in the meantime."""
import json
import os
import sqlite3
import tempfile
import unittest

from bot import ml, train_delta
from bot.storage import Store


class TestTrainDelta(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        base = os.path.join(self.d.name, "base.db")
        st = Store(base)                      # the memory both jobs start from
        st.set_kv("model_status", "old")
        st.set_kv("bot_owned", 1)
        st.x("INSERT INTO bets(ts,mode,ticker,status) VALUES(1,'paper','KXBTC15M-OLD','graded')")
        st.db.close()
        self.trainer, self.bot = os.path.join(self.d.name, "trainer.db"), os.path.join(self.d.name, "bot.db")
        for p in (self.trainer, self.bot):
            with open(base, "rb") as a, open(p, "wb") as b:
                b.write(a.read())
        self.mark = os.path.join(self.d.name, "mark.json")
        train_delta.mark(self.trainer, self.mark)
        # --- the trainer's work (its own copy) ---
        st = Store(self.trainer)
        ml.ensure(st) if hasattr(ml, "ensure") else None
        ml.add_row(st, "KXBTC15M-H1", 100, "BTC", {"a": 1}, 1, "history")
        ml.add_row(st, "KXBTC15M-H2", 200, "BTC", {"a": 2}, 0, "history")
        st.set_weight("crypto_model@crypto", 0.9, "history")
        st.set_kv("model_status", "new")
        st.db.close()
        # --- meanwhile the bot keeps trading on the live memory ---
        st = Store(self.bot)
        st.x("INSERT INTO bets(ts,mode,ticker,status) VALUES(2,'paper','KXBTC15M-LIVE','open')")
        st.set_kv("bot_owned", 2)
        st.db.close()
        self.delta = os.path.join(self.d.name, "delta.sqlite")
        self.counts = train_delta.export(self.trainer, self.mark, self.delta, "run-1")

    def tearDown(self):
        self.d.cleanup()

    def q(self, sql):
        db = sqlite3.connect(self.bot)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def test_export_holds_only_what_training_added(self):
        self.assertEqual(self.counts["ml_rows"], 2)
        self.assertEqual(self.counts["kv"], 1)                   # model_status only
        self.assertNotIn("bets", self.counts)

    def test_import_adds_training_and_keeps_the_bots_own_work(self):
        models = os.path.join(self.d.name, "models_src")
        os.makedirs(models)
        with open(os.path.join(models, "crypto_model.json"), "w") as f:
            json.dump({"trained": "run-1"}, f)
        dst = os.path.join(self.d.name, "models_dst")
        got = train_delta.import_delta(self.bot, self.delta, models_src=models, models_dst=dst)
        self.assertEqual(got["ml_rows"], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM ml_rows WHERE source='history'")[0][0], 2)
        self.assertEqual(self.q("SELECT v FROM kv WHERE k='model_status'")[0][0], json.dumps("new"))
        self.assertEqual(self.q("SELECT v FROM kv WHERE k='bot_owned'")[0][0], json.dumps(2))     # untouched
        self.assertEqual(sorted(r[0] for r in self.q("SELECT ticker FROM bets")),
                         ["KXBTC15M-LIVE", "KXBTC15M-OLD"])                                      # untouched
        self.assertTrue(os.path.exists(os.path.join(dst, "crypto_model.json")))
        self.assertTrue(self.q("SELECT 1 FROM weight_history WHERE signal='crypto_model@crypto'"))

    def test_imports_once(self):
        train_delta.import_delta(self.bot, self.delta)
        self.assertIsNone(train_delta.import_delta(self.bot, self.delta))
        self.assertEqual(self.q("SELECT COUNT(*) FROM ml_rows")[0][0], 2)

    def test_a_broken_delta_changes_nothing(self):
        # kv is applied after the training rows; a malformed kv table fails mid-import
        db = sqlite3.connect(self.delta)
        db.execute("DROP TABLE kv")
        db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, wrong TEXT)")
        db.commit()
        db.close()
        before = self.q("SELECT k, v FROM kv ORDER BY k")
        with self.assertRaises(sqlite3.Error):
            train_delta.import_delta(self.bot, self.delta)
        self.assertEqual(self.q("SELECT k, v FROM kv ORDER BY k"), before)
        self.assertFalse(self.q("SELECT name FROM sqlite_master WHERE name='ml_rows'")
                         and self.q("SELECT COUNT(*) FROM ml_rows")[0][0])


if __name__ == "__main__":
    unittest.main()
