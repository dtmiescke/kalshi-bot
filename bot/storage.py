"""SQLite storage. Append-only by design: the bot never deletes what it learned.

Weight changes are written as new history rows; the current weight is the latest row.
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    ts INTEGER, ticker TEXT, event_ticker TEXT, series TEXT, title TEXT,
    team TEXT, yes_bid REAL, yes_ask REAL, last_price REAL,
    volume REAL, open_interest REAL, close_time TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap ON market_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    ts INTEGER, sport TEXT, event_id TEXT, commence_time TEXT,
    home TEXT, away TEXT, team TEXT, fair_prob REAL, n_books INTEGER,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_odds ON odds_snapshots(event_id, team, ts);

CREATE TABLE IF NOT EXISTS flow_trades (
    trade_id TEXT PRIMARY KEY, ticker TEXT, ts INTEGER, count REAL,
    yes_price REAL, taker_side TEXT
);
CREATE INDEX IF NOT EXISTS ix_flow ON flow_trades(ticker, ts);

CREATE TABLE IF NOT EXISTS signal_readings (
    ts INTEGER, ticker TEXT, signal TEXT, prob REAL, confidence REAL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig ON signal_readings(ticker, ts);

CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, mode TEXT, ticker TEXT, event_ticker TEXT, series TEXT,
    team TEXT, side TEXT, price REAL, contracts INTEGER, stake REAL, fee REAL,
    model_prob REAL, edge REAL, confidence REAL, signals_json TEXT,
    order_id TEXT, status TEXT DEFAULT 'open',
    closing_price REAL, result TEXT, pnl REAL, clv REAL, graded_ts INTEGER
);

CREATE TABLE IF NOT EXISTS weight_history (
    ts INTEGER, signal TEXT, weight REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS signal_grades (
    ts INTEGER, bet_id INTEGER, signal TEXT, prob REAL, outcome INTEGER,
    brier REAL, clv_direction INTEGER, segment TEXT
);

CREATE TABLE IF NOT EXISTS graded_markets (
    ticker TEXT PRIMARY KEY, ts INTEGER, outcome INTEGER, series TEXT
);

CREATE TABLE IF NOT EXISTS lessons (
    ts INTEGER, model TEXT, text TEXT
);

CREATE TABLE IF NOT EXISTS maker_attempts (
    ts INTEGER, ticker TEXT, filled INTEGER, outcome TEXT, wait_s REAL
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

-- Decision audit is not a trade ledger: rejected candidates never become fake bets.
CREATE TABLE IF NOT EXISTS decision_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, ticker TEXT NOT NULL,
    event_ticker TEXT, series TEXT, decision TEXT NOT NULL, reason TEXT,
    side TEXT, market_price REAL, model_prob REAL, side_prob REAL, edge REAL,
    confidence REAL, signals_json TEXT, outcome INTEGER, brier REAL,
    hypothetical_pnl REAL, settled_ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_decision_audit_ticker_ts ON decision_audit(ticker, ts);
CREATE INDEX IF NOT EXISTS ix_decision_audit_grade ON decision_audit(outcome, decision);
"""


class Store:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        # shared across the live runner's threads, guarded by one lock
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(bets)")}
        for col, typ in (("exit_price", "REAL"), ("exit_ts", "INTEGER"), ("style", "TEXT")):
            if col not in cols:   # upgrade older databases in place
                self.db.execute(f"ALTER TABLE bets ADD COLUMN {col} {typ}")
        self.db.commit()

    def add_decision_audit(self, row):
        """Record one decision snapshot; this table never creates a bet/order."""
        self.x("""INSERT INTO decision_audit
            (ts,ticker,event_ticker,series,decision,reason,side,market_price,
             model_prob,side_prob,edge,confidence,signals_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            int(row.get("ts", time.time())), row["ticker"], row.get("event_ticker"),
            row.get("series"), row["decision"], row.get("reason"), row.get("side"),
            row.get("market_price"), row.get("model_prob"), row.get("side_prob"),
            row.get("edge"), row.get("confidence"),
            json.dumps(row.get("signals", {}), ensure_ascii=False, default=str)))

    def grade_decision_audit(self, ticker, outcome, settled_ts=None):
        """Grade recorded snapshots without rewriting what the bot knew at decision time."""
        settled_ts = int(settled_ts or time.time())
        with self.lock:
            rows = self.db.execute("SELECT id, side_prob, market_price, side, decision FROM decision_audit WHERE ticker=? AND outcome IS NULL", (ticker,)).fetchall()
            for row in rows:
                if row["side_prob"] is None:
                    continue
                y = int(outcome)
                brier = (float(row["side_prob"]) - y) ** 2
                pnl = None
                if row["decision"] == "candidate" and row["side"] in ("yes", "no") and row["market_price"] is not None:
                    p = float(row["market_price"])
                    won = (row["side"] == "yes" and y == 1) or (row["side"] == "no" and y == 0)
                    pnl = (1 - p) if won else -p
                self.db.execute("UPDATE decision_audit SET outcome=?, brier=?, hypothetical_pnl=?, settled_ts=? WHERE id=?", (y, brier, pnl, settled_ts, row["id"]))
            self.db.commit()
            return len(rows)

    def decision_audit_summary(self):
        return self.q("SELECT decision, COUNT(*) AS n, AVG(brier) AS brier, AVG(hypothetical_pnl) AS diagnostic_pnl FROM decision_audit WHERE outcome IS NOT NULL GROUP BY decision ORDER BY decision")

    # --- generic -----------------------------------------------------------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def get_kv(self, k, default=None):
        rows = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(rows[0]["v"]) if rows else default

    def set_kv(self, k, v):
        self.x("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v)))

    def compact(self, keep_days=None):
        """Trim RAW data so the memory file stays small enough to save to GitHub.
        Learned things (weights, grades, bets, lessons, model runs) are never
        trimmed; raw snapshots can be re-downloaded from Kalshi any time."""
        keep_days = keep_days or {"market_snapshots": 14, "flow_trades": 7,
                                  "signal_readings": 30, "odds_snapshots": 30, "ml_rows": 60}
        now = int(time.time())
        with self.lock:
            for table, days in keep_days.items():
                try:
                    self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (now - days * 86400,))
                except sqlite3.OperationalError:
                    pass
            self.db.commit()
            self.db.execute("VACUUM")

    # --- snapshots ---------------------------------------------------------
    def add_market_snapshot(self, m):
        self.x("""INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            m["ts"], m["ticker"], m["event_ticker"], m["series"], m["title"],
            m.get("team"), m.get("yes_bid"), m.get("yes_ask"), m.get("last_price"),
            m.get("volume"), m.get("open_interest"), m.get("close_time"), m.get("status")))

    def price_history(self, ticker, since_ts=0):
        return self.q("""SELECT ts, yes_bid, yes_ask, last_price, volume
                         FROM market_snapshots WHERE ticker=? AND ts>=? ORDER BY ts""",
                      (ticker, since_ts))

    def add_odds(self, row):
        self.x("""INSERT INTO odds_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)""", (
            row["ts"], row["sport"], row["event_id"], row["commence_time"],
            row["home"], row["away"], row["team"], row["fair_prob"],
            row["n_books"], json.dumps(row.get("raw", {}))))

    def odds_history(self, event_id, team):
        rows = self.q("""SELECT ts, fair_prob, n_books, raw_json FROM odds_snapshots
                         WHERE event_id=? AND team=? ORDER BY ts""", (event_id, team))
        for r in rows:
            r["raw"] = json.loads(r.pop("raw_json") or "{}")
        return rows

    def add_flow_trades(self, trades):
        with self.lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO flow_trades VALUES(?,?,?,?,?,?)",
                [(t["trade_id"], t["ticker"], t["ts"], t["count"], t["yes_price"],
                  t["taker_side"]) for t in trades])
            self.db.commit()

    def flow(self, ticker, since_ts):
        return self.q("""SELECT * FROM flow_trades WHERE ticker=? AND ts>=?
                         ORDER BY ts""", (ticker, since_ts))

    def add_signal(self, ts, ticker, name, prob, conf, detail):
        self.x("INSERT INTO signal_readings VALUES(?,?,?,?,?,?)",
               (ts, ticker, name, prob, conf, json.dumps(detail)))

    # --- weights (append-only) ---------------------------------------------
    def current_weights(self):
        rows = self.q("""SELECT signal, weight FROM weight_history w
                         WHERE ts = (SELECT MAX(ts) FROM weight_history
                                     WHERE signal = w.signal)""")
        return {r["signal"]: r["weight"] for r in rows}

    def set_weight(self, signal, weight, reason):
        # time.time_ns keeps ordering strict even within one second
        self.x("INSERT INTO weight_history VALUES(?,?,?,?)",
               (time.time_ns(), signal, weight, reason))

    # --- bets ----------------------------------------------------------------
    def add_bet(self, b):
        cur = self.x("""INSERT INTO bets(ts, mode, ticker, event_ticker, series, team,
                        side, price, contracts, stake, fee, model_prob, edge,
                        confidence, signals_json, order_id, style)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            b["ts"], b["mode"], b["ticker"], b["event_ticker"], b["series"], b["team"],
            b["side"], b["price"], b["contracts"], b["stake"], b["fee"],
            b["model_prob"], b["edge"], b["confidence"], json.dumps(b["signals"]),
            b.get("order_id"), b.get("style")))
        return cur.lastrowid

    def split_bet(self, bet_id, sold):
        """Partial sale: shrink the open bet to the unsold part and return the id
        of a new row holding the sold part (which is then closed)."""
        b = self.q("SELECT * FROM bets WHERE id=?", (bet_id,))[0]
        keep = b["contracts"] - sold
        frac = sold / b["contracts"]
        self.x("UPDATE bets SET contracts=?, stake=?, fee=? WHERE id=?",
               (keep, round(b["stake"] * (1 - frac), 2), round(b["fee"] * (1 - frac), 2), bet_id))
        cols = [c for c in b if c not in ("id",)]
        vals = [b[c] for c in cols]
        row = dict(zip(cols, vals), contracts=sold, stake=round(b["stake"] * frac, 2),
                   fee=round(b["fee"] * frac, 2))
        with self.lock:
            cur = self.db.execute(f"INSERT INTO bets({','.join(row)}) VALUES({','.join('?' * len(row))})",
                                  list(row.values()))
            self.db.commit()
        return cur.lastrowid

    def log_maker(self, ticker, filled, outcome, wait_s):
        self.x("INSERT INTO maker_attempts VALUES(?,?,?,?,?)",
               (int(time.time()), ticker, int(filled), outcome, wait_s))

    def maker_fill_rate(self, n=30):
        rows = self.q("SELECT filled FROM maker_attempts ORDER BY ts DESC LIMIT ?", (n,))
        return (sum(r["filled"] for r in rows) / len(rows), len(rows)) if rows else (None, 0)

    def close_bet(self, bet_id, exit_price, pnl):
        """Sold before settlement: realised P&L, no longer open."""
        self.x("""UPDATE bets SET status='graded', result='sold', exit_price=?, exit_ts=?,
                  pnl=?, graded_ts=? WHERE id=?""",
               (exit_price, int(time.time()), round(pnl, 2), int(time.time()), bet_id))

    def open_bets(self):
        return self.q("SELECT * FROM bets WHERE status='open'")

    def staked_since(self, ts):
        r = self.q("SELECT COALESCE(SUM(stake),0) s FROM bets WHERE ts>=?", (ts,))
        return r[0]["s"]

    def staked_on_event(self, event_ticker):
        r = self.q("SELECT COALESCE(SUM(stake),0) s FROM bets WHERE event_ticker=?",
                   (event_ticker,))
        return r[0]["s"]

    def account(self, bankroll):
        """Virtual (paper) or tracked (live) account: cash can never go negative."""
        r = self.q("""SELECT
            COALESCE(SUM(CASE WHEN status='graded' THEN pnl END), 0) realized,
            COALESCE(SUM(CASE WHEN status='open' THEN stake + fee END), 0) open_cost,
            COALESCE(SUM(CASE WHEN status='open' THEN 1 END), 0) open_n FROM bets""")[0]
        return {"cash": round(bankroll + r["realized"] - r["open_cost"], 2),
                "realized": round(r["realized"], 2), "open_cost": round(r["open_cost"], 2),
                "open_positions": int(r["open_n"]), "equity_at_cost": round(bankroll + r["realized"], 2)}

    def realized_today(self, day_start):
        r = self.q("SELECT COALESCE(SUM(pnl),0) p FROM bets WHERE status='graded' AND graded_ts>=?",
                   (day_start,))
        return r[0]["p"]

    def has_open_event_bet(self, event_ticker):
        return bool(self.q("SELECT 1 FROM bets WHERE event_ticker=? AND status='open'",
                           (event_ticker,)))

    def has_open_bet(self, ticker):
        return bool(self.q("SELECT 1 FROM bets WHERE ticker=? AND status='open'", (ticker,)))
