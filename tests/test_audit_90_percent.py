import sqlite3
import unittest

from scripts.audit_90_percent import evaluate_connection, _wilson_lower


class Audit90PercentTests(unittest.TestCase):
    def make_db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE decision_audit (
                id INTEGER PRIMARY KEY, ticker TEXT, model_prob REAL, outcome INTEGER,
                brier REAL, settled_ts INTEGER
            );
            CREATE TABLE bets (
                id INTEGER PRIMARY KEY, mode TEXT, side TEXT, result TEXT, status TEXT,
                pnl REAL, ticker TEXT, fee REAL, stake REAL, price REAL, contracts INTEGER,
                graded_ts INTEGER
            );
        """)
        return db

    def test_empty_ledger_is_not_a_pass(self):
        result = evaluate_connection(self.make_db())
        self.assertEqual(result["prediction_accuracy"]["n"], 0)
        self.assertFalse(result["verdict"]["prediction_target_supported"])
        self.assertFalse(result["verdict"]["paper_bet_target_supported"])

    def test_repeated_unscored_forecasts_are_excluded(self):
        db = self.make_db()
        db.execute("INSERT INTO decision_audit VALUES(1,'A',0.99,1,0.0001,100)")
        db.execute("INSERT INTO decision_audit VALUES(2,'A',0.99,0,NULL,101)")
        result = evaluate_connection(db)
        self.assertEqual(result["prediction_accuracy"]["n"], 1)
        self.assertEqual(result["high_confidence_prediction_accuracy"]["n"], 1)

    def test_only_settled_paper_bets_count(self):
        db = self.make_db()
        db.execute("INSERT INTO bets VALUES(1,'paper','yes','yes','graded',0.1,'A',0.01,1,0.4,1,100)")
        db.execute("INSERT INTO bets VALUES(2,'live','yes','yes','graded',0.1,'B',0.01,1,0.4,1,100)")
        db.execute("INSERT INTO bets VALUES(3,'paper','no','yes','open',-0.4,'C',0.01,1,0.4,1,100)")
        result = evaluate_connection(db)
        self.assertEqual(result["paper_bet_win_rate"]["n"], 1)
        self.assertEqual(result["paper_bet_win_rate"]["rate"], 1.0)
        self.assertAlmostEqual(result["paper_net_pnl"], 0.1)

    def test_90_percent_claim_requires_minimum_sample_and_lower_bound(self):
        db = self.make_db()
        for i in range(100):
            db.execute("INSERT INTO decision_audit VALUES(?,?,?,?,?,?)",
                       (i + 1, f"T{i}", 0.99, 1 if i < 95 else 0,
                        (0.99 - (1 if i < 95 else 0)) ** 2, 100 + i))
        result = evaluate_connection(db)
        self.assertEqual(result["prediction_accuracy"]["n"], 100)
        self.assertFalse(result["verdict"]["prediction_target_supported"])

    def test_wilson_bound_handles_empty_and_perfect_samples(self):
        self.assertIsNone(_wilson_lower(0, 0))
        self.assertLess(_wilson_lower(9, 10), 0.9)
        self.assertGreater(_wilson_lower(1000, 1000), 0.99)


if __name__ == "__main__":
    unittest.main()
