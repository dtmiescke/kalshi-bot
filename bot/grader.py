"""Grading: settle bets, measure closing-line value, and teach the brain.

The brain learns from EVERY market the bot researched, not just the ones it
bet — far more data, so it gets smarter much faster.
"""
import json
import logging
import time

from . import brain, hindsight, ml, window_review

log = logging.getLogger("bot")


def closing_yes(store, ticker, start_ts):
    """Last Kalshi mid we saw before the game started = the closing line."""
    rows = store.q("""SELECT yes_bid, yes_ask, last_price FROM market_snapshots
                      WHERE ticker=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
                   (ticker, start_ts or int(time.time())))
    if not rows:
        return None
    r = rows[0]
    if r["yes_bid"] is not None and r["yes_ask"]:
        return (r["yes_bid"] + r["yes_ask"]) / 2
    return r["last_price"]


def _rotating_tickers(store, source, cursor_key, max_fetch):
    """Fetch a bounded, rotating batch so ungraded rows cannot starve later ones."""
    limit = max(1, int(max_fetch))
    cursor = str(store.get_kv(cursor_key, "") or "")
    if source == "signals":
        after_sql = """SELECT DISTINCT s.ticker FROM signal_readings s
                       LEFT JOIN graded_markets g ON g.ticker=s.ticker
                       WHERE g.ticker IS NULL AND s.ticker>? ORDER BY s.ticker LIMIT ?"""
        wrap_sql = """SELECT DISTINCT s.ticker FROM signal_readings s
                      LEFT JOIN graded_markets g ON g.ticker=s.ticker
                      WHERE g.ticker IS NULL AND s.ticker<=? ORDER BY s.ticker LIMIT ?"""
    elif source == "decision_audit":
        after_sql = "SELECT DISTINCT ticker FROM decision_audit WHERE outcome IS NULL AND ticker>? ORDER BY ticker LIMIT ?"
        wrap_sql = "SELECT DISTINCT ticker FROM decision_audit WHERE outcome IS NULL AND ticker<=? ORDER BY ticker LIMIT ?"
    elif source == "combo_prediction_audit":
        after_sql = "SELECT DISTINCT ticker FROM combo_prediction_audit WHERE outcome IS NULL AND ticker>? ORDER BY ticker LIMIT ?"
        wrap_sql = "SELECT DISTINCT ticker FROM combo_prediction_audit WHERE outcome IS NULL AND ticker<=? ORDER BY ticker LIMIT ?"
    elif source == "council_votes":
        after_sql = "SELECT DISTINCT ticker FROM council_votes WHERE outcome IS NULL AND ticker>? ORDER BY ticker LIMIT ?"
        wrap_sql = "SELECT DISTINCT ticker FROM council_votes WHERE outcome IS NULL AND ticker<=? ORDER BY ticker LIMIT ?"
    else:
        raise ValueError(f"unknown grading source: {source}")
    rows = store.q(after_sql, (cursor, limit))
    if len(rows) < limit:
        rows.extend(store.q(wrap_sql, (cursor, limit - len(rows))))
    return [row["ticker"] for row in rows]


def _advance_cursor(store, cursor_key, tickers):
    if tickers:
        store.set_kv(cursor_key, tickers[-1])


def settle_bets(store, client):
    done = 0
    for b in store.open_bets():
        try:
            m = client.market(b["ticker"])
        except Exception as e:
            log.warning("market %s: %s", b["ticker"], e)
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        won = result == b["side"]
        pnl = (b["contracts"] * (1 - b["price"]) if won
               else -b["contracts"] * b["price"]) - b["fee"]
        close = closing_yes(store, b["ticker"], store.get_kv(f"start_{b['ticker']}"))
        clv = None
        if close is not None:
            close_side = close if b["side"] == "yes" else 1 - close
            clv = close_side - b["price"]
        # Conditional update makes concurrent grading passes idempotent:
        # only a worker that still owns an open row may finalize its result.
        with store.lock:
            cur = store.db.execute("""UPDATE bets SET status='graded', result=?, pnl=?,
                closing_price=?, clv=?, graded_ts=? WHERE id=? AND status='open'""",
                (result, round(pnl, 2), close, clv, int(time.time()), b["id"]))
            store.db.commit()
        if cur.rowcount == 1:
            done += 1
            # Persist an auditable postmortem only after the ledger is graded.
            # Review creation is idempotent and never changes execution or sizing.
            try:
                from .coin_learning import persist_trade_review
                reviewed = dict(b)
                reviewed.update(result=result, pnl=round(pnl, 2), closing_price=close,
                                clv=clv, graded_ts=int(time.time()), status="graded")
                persist_trade_review(store, reviewed)
            except Exception:
                log.exception("trade postmortem failed for bet id=%s", b.get("id"))
    return done


def learn_from_markets(store, client, max_fetch=150):
    """Grade every researched market that has settled, then update weights."""
    now = int(time.time())
    rotating = _rotating_tickers(store, "signals", "learning_cursor_ticker", max_fetch)
    # 15-minute markets settle five minutes after they close and everything downstream (the scoreboard, the
    # checklist, the learning) waits for them. The rotation above is alphabetical over thousands of sports futures
    # that will not settle for weeks, so a full sweep took about 3.5 hours and each 15-minute market was graded
    # that late. They go first on every pass; the rotation, and its cursor, carry on for everything else.
    priority = [r["ticker"] for r in store.q(
        """SELECT DISTINCT s.ticker FROM signal_readings s LEFT JOIN graded_markets g ON g.ticker=s.ticker
           WHERE g.ticker IS NULL AND s.ticker LIKE 'KX%15M-%' ORDER BY s.ticker LIMIT ?""", (int(max_fetch),))]
    first = set(priority)
    tickers = priority + [t for t in rotating if t not in first]
    graded = []
    for t in tickers:
        start = store.get_kv(f"start_{t}")
        settle_after = store.get_kv(f"settle_after_{t}") or (start + 3 * 3600 if start else None)
        if not start or now < settle_after:
            continue   # game / window not finished yet
        try:
            m = client.market(t)
        except Exception:
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        y = 1 if result == "yes" else 0
        try:
            store.grade_decision_audit(t, y, now, brain.decision_cutoff(t, start))
        except Exception as e:
            log.warning("decision audit grading failed for %s: %s", t, e)
        ml.label(store, t, y)      # live decisions become training rows
        try:                       # and the window's opening read gets what really happened
            window_review.settle(store, t, y, now)
            hindsight.review_window(store, t, y)      # which setup would have worked?
        except Exception as e:
            log.warning("window review failed for %s: %s", t, e)
        # the last reading of each signal before the game started
        rows = store.q("""SELECT signal, prob FROM signal_readings r
                          WHERE ticker=? AND ts<=? AND ts = (
                            SELECT MAX(ts) FROM signal_readings
                            WHERE ticker=r.ticker AND signal=r.signal AND ts<=?)""",
                       (t, brain.decision_cutoff(t, start), brain.decision_cutoff(t, start)))
        if not rows:
            store.x("INSERT OR IGNORE INTO graded_markets VALUES(?,?,?,?)",
                    (t, now, y, m.get("series_ticker") or m.get("series") or ""))
            continue
        g = {r["signal"]: r["prob"] for r in rows}
        g["_outcome"] = y
        g["_domain"] = brain.learning_domain_of(m.get("series_ticker") or t)
        graded.append(g)
        for r in rows:
            store.x("INSERT INTO signal_grades VALUES(?,?,?,?,?,?,?,?)",
                    (now, None, r["signal"], r["prob"], y, (r["prob"] - y) ** 2, None,
                     t))
        store.x("INSERT OR IGNORE INTO graded_markets VALUES(?,?,?,?)",
                (t, now, y, m.get("series_ticker") or m.get("series") or ""))
    _advance_cursor(store, "learning_cursor_ticker", rotating)       # the cursor follows the rotation, not the priority list
    changes = {}
    # Learn independently by identifiable league. Unknown leagues stay in the
    # shared sports bucket; crypto remains isolated from all sports weights.
    domains = sorted({g["_domain"] for g in graded})
    for dom in domains:
        batch = [g for g in graded if g["_domain"] == dom]
        if batch:
            changes.update({f"{k}@{dom}": v for k, v in brain.learn(store, batch, dom).items()})
    return len(graded), changes


def grade_decision_audits(store, client, max_fetch=150):
    """Grade logged candidates and rejections from real Kalshi settlements only."""
    tickers = _rotating_tickers(store, "decision_audit", "decision_grade_cursor_ticker", max_fetch)
    graded = 0
    for ticker in tickers:
        try:
            market = client.market(ticker)
        except Exception as e:
            log.debug("decision audit market lookup %s: %s", ticker, e)
            continue
        result = market.get("result")
        if result not in ("yes", "no"):
            continue
        start = store.get_kv(f"start_{ticker}")
        cutoff = brain.decision_cutoff(ticker, start) if start else 0
        graded += store.grade_decision_audit(
            ticker, 1 if result == "yes" else 0, cutoff_ts=cutoff)
    _advance_cursor(store, "decision_grade_cursor_ticker", tickers)
    return graded


COUNCIL_VOTE_KEEP_DAYS = 180


def grade_council_votes(store, client, max_fetch=60, now=None):
    """Score council member votes from real Kalshi settlements (drives seating,
    the doghouse and earned power). Old graded votes are dropped after 180 days."""
    tickers = _rotating_tickers(store, "council_votes", "members_grade_cursor_ticker", max_fetch)
    graded = 0
    for ticker in tickers:
        try:
            result = client.market(ticker).get("result")
        except Exception as e:
            log.debug("council vote market lookup %s: %s", ticker, e)
            continue
        if result in ("yes", "no"):
            graded += store.grade_council_votes(ticker, 1 if result == "yes" else 0, settled_ts=now)
    _advance_cursor(store, "members_grade_cursor_ticker", tickers)
    cutoff = int(now or time.time()) - COUNCIL_VOTE_KEEP_DAYS * 86400
    store.x("DELETE FROM council_votes WHERE outcome IS NOT NULL AND settled_ts < ?", (cutoff,))
    return graded


def monitor_model(store, window=300):
    """LIVE DEMOTION: the model passed its tests once, but markets change. If over
    the last `window` settled crypto markets the model's live forecasts are worse
    than Kalshi's own price, retire it automatically (it's kept, not deleted)."""
    import os
    # Compare the model and Kalshi on the SAME 15-minute contract only.
    # Sports outcomes must not trigger demotion of the crypto model, and using
    # event_ticker as the key merges multiple strikes into one misleading pair.
    rows = store.q("""SELECT segment, signal, brier FROM signal_grades
                      WHERE signal IN ('ml_model', 'kalshi_market')
                        AND segment LIKE '%15M%'
                      ORDER BY ts DESC, rowid DESC LIMIT ?""", (window * 2,))
    by = {}
    for r in rows:
        by.setdefault(r["segment"], {})[r["signal"]] = r["brier"]
    pairs = [(v["ml_model"], v["kalshi_market"]) for v in by.values()
             if "ml_model" in v and "kalshi_market" in v]
    if len(pairs) < window // 2:
        return None
    ml_b = sum(a for a, _ in pairs) / len(pairs)
    k_b = sum(b for _, b in pairs) / len(pairs)
    if ml_b <= k_b:
        return {"ok": True, "ml_brier": ml_b, "kalshi_brier": k_b, "n": len(pairs)}
    src = os.path.join(ml.MODEL_DIR, "crypto_model.json")
    if os.path.exists(src):
        stamp = int(time.time())
        for ext in ("joblib", "json"):
            p = os.path.join(ml.MODEL_DIR, f"crypto_model.{ext}")
            if os.path.exists(p):
                os.replace(p, os.path.join(ml.MODEL_DIR, f"demoted_{stamp}.{ext}"))
        ml._CACHE.clear()
        from . import alerts
        msg = (f"Live Brier {ml_b:.4f} vs Kalshi {k_b:.4f} over {len(pairs)} markets. "
               "Model retired; formula + learned weights continue.")
        alerts.send("Model demoted", msg, priority="high", tags=["warning"])
        log.warning("MODEL DEMOTED: %s", msg)
    return {"ok": False, "ml_brier": ml_b, "kalshi_brier": k_b, "n": len(pairs)}


def grade_combo_predictions(store, client, max_fetch=150):
    """Grade combo forecasts only after Kalshi reports actual settled YES/NO."""
    tickers = _rotating_tickers(store, "combo_prediction_audit", "combo_grade_cursor_ticker", max_fetch)
    graded = 0
    for ticker in tickers:
        try:
            market = client.market(ticker)
        except Exception as exc:
            log.debug("combo audit market lookup %s: %s", ticker, exc)
            continue
        result = market.get("result")
        if result in ("yes", "no"):
            graded += store.grade_combo_prediction(ticker, 1 if result == "yes" else 0)
    _advance_cursor(store, "combo_grade_cursor_ticker", tickers)
    return graded


def _settle_gap_summary(samples, min_samples=30):
    """{source: {coin: gap stats in bps}}; recommends a SETTLE_PROXY_BPS only
    with enough data."""
    return {src: _settle_gap_coins([x for x in samples if x.get("source", "bot_readings") == src],
                                   min_samples)
            for src in sorted({x.get("source", "bot_readings") for x in samples})}


def _settle_gap_coins(samples, min_samples):
    out = {}
    for coin in sorted({x["coin"] for x in samples}):
        gaps = sorted(x["gap_bps"] for x in samples if x["coin"] == coin)
        n = len(gaps)
        rms = (sum(g * g for g in gaps) / n) ** 0.5
        abs_sorted = sorted(abs(g) for g in gaps)
        out[coin] = {
            "n": n, "mean_bps": round(sum(gaps) / n, 2), "median_bps": round(gaps[n // 2], 2),
            "rms_bps": round(rms, 2), "p90_abs_bps": round(abs_sorted[min(n - 1, int(0.9 * n))], 2),
            # the proxy term is a standard deviation in bps; RMS includes any bias
            "recommended_proxy_bps": round(rms, 1) if n >= min_samples else None,
        }
    return out


def settlement_gap(store, client, max_fetch=150, now=None, keep=3000):
    """Read-only T1c check: how far is our spot price from the index Kalshi
    settles 15-minute crypto on?

    For each settled window, the crypto_model readings the bot recorded inside
    the final averaging window (avg_s, default 60 s) are averaged and compared
    with Kalshi's expiration_value ("the value that was considered for the
    settlement": the CF Benchmarks average). gap_bps = (ours / index - 1) * 1e4.
    Changes no setting; the report (kv settle_gap_report) says whether
    SETTLE_PROXY_BPS looks too small.
    """
    now = int(time.time() if now is None else now)
    samples = store.get_kv("settle_gap_samples") or []
    done = {x["ticker"] for x in samples if x.get("source", "bot_readings") == "bot_readings"}
    skipped = {"no_final_minute_readings": 0, "no_expiration_value": 0, "lookup_failed": 0}
    rows = store.q("""SELECT DISTINCT ticker FROM signal_readings
                      WHERE signal='crypto_model' AND ticker LIKE 'KX%15M-%'""")
    todo = []
    for r in rows:
        t = r["ticker"]
        close = store.get_kv(f"start_{t}")
        if t not in done and close and now >= int(close) + 300:
            todo.append((int(close), t))
    for close, t in sorted(todo, reverse=True)[:max(0, int(max_fetch))]:
        readings = store.q("""SELECT detail FROM signal_readings WHERE ticker=? AND
                              signal='crypto_model' AND ts BETWEEN ? AND ?""", (t, close - 120, close))
        spots = []
        for row in readings:
            d = json.loads(row["detail"] or "{}")
            avg_s = d.get("avg_s") or 60
            secs_left = d.get("secs_left")
            if isinstance(d.get("spot"), (int, float)) and d["spot"] > 0 and \
                    secs_left is not None and 0 <= secs_left <= avg_s:
                spots.append(float(d["spot"]))
        if not spots:
            skipped["no_final_minute_readings"] += 1
            continue
        try:
            m = client.market(t)
        except Exception as e:
            log.debug("settlement gap lookup %s: %s", t, e)
            skipped["lookup_failed"] += 1
            continue
        try:
            index = float(m.get("expiration_value"))
        except (TypeError, ValueError):
            index = 0.0
        if m.get("result") not in ("yes", "no") or not index > 0:
            skipped["no_expiration_value"] += 1
            continue
        ours = sum(spots) / len(spots)
        samples.append({"source": "bot_readings", "ticker": t, "coin": ml.coin_of(t), "close_ts": close,
                        "readings": len(spots), "ours": round(ours, 4), "index": index,
                        "gap_bps": round((ours / index - 1) * 1e4, 3)})
    return _save_settle_gap(store, samples, skipped, now, keep)


def _save_settle_gap(store, samples, skipped, now, keep):
    import os
    samples = sorted(samples, key=lambda x: x["close_ts"])[-keep:]
    store.set_kv("settle_gap_samples", samples)
    report = {"ts": now, "samples": len(samples), "skipped_this_run": skipped,
              "current_proxy_bps": float(os.environ.get("SETTLE_PROXY_BPS", 3)),
              "by_source": _settle_gap_summary(samples)}
    store.set_kv("settle_gap_report", report)
    return report


def replay_settlement_gap(store, client, days=3, now=None, get=None, keep=3000):
    """Same check over many past windows: Coinbase's 1-minute candle for the
    final averaging minute (mean of open/high/low/close, the feed the bot
    prices from) vs Kalshi's expiration_value. Read-only."""
    import requests
    from . import history
    from .config import CRYPTO_15M
    from .live.settlement import contract_terms
    get = get or requests.get
    now = int(time.time() if now is None else now)
    start = now - int(days * 86400)
    samples = store.get_kv("settle_gap_samples") or []
    done = {(x.get("source", "bot_readings"), x["ticker"]) for x in samples}
    skipped = {"no_expiration_value": 0, "not_60s_average": 0, "no_coinbase_minute": 0}
    for series, pair in CRYPTO_15M.items():
        markets = history.settled_markets(client, series, start, now, limit=int(days * 96) + 10)
        if not markets:
            continue
        candles = history.spot_candles(pair, start - 120, now, get)
        for m in markets:
            if ("coinbase_replay", m["ticker"]) in done:
                continue
            terms = contract_terms(m)
            try:
                index = float(m.get("expiration_value"))
            except (TypeError, ValueError):
                index = 0.0
            if not index > 0:
                skipped["no_expiration_value"] += 1
                continue
            if terms["avg_seconds"] != 60:
                skipped["not_60s_average"] += 1
                continue
            c = candles.get(int(terms["close_ts"]) - 60)   # candles are stamped at minute start
            if not c:
                skipped["no_coinbase_minute"] += 1
                continue
            ours = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
            samples.append({"source": "coinbase_replay", "ticker": m["ticker"],
                            "coin": ml.coin_of(m["ticker"]), "close_ts": int(terms["close_ts"]),
                            "ours": round(ours, 4), "index": index,
                            "gap_bps": round((ours / index - 1) * 1e4, 3)})
    return _save_settle_gap(store, samples, skipped, now, keep)


def grade(store, client):
    from .kalshi_history import GradingClient
    # live first, then Kalshi's archive (settled markets move there after a cutoff)
    client = GradingClient(client)
    n_bets = settle_bets(store, client)
    n_mkts, changes = learn_from_markets(store, client)
    n_decisions = grade_decision_audits(store, client)
    n_combos = grade_combo_predictions(store, client)
    n_votes = grade_council_votes(store, client)
    monitor_model(store)
    store.set_kv("grading_lookup_health", dict(client.health(), ts=int(time.time())))
    log.info("graded %d bets, %d markets, %d decision snapshots, %d combo forecasts, weight changes: %s", n_bets, n_mkts, n_decisions, n_combos,
             json.dumps({k: [round(a, 3), round(b, 3)] for k, (a, b) in changes.items()}))
    return {"bets": n_bets, "markets": n_mkts, "combo_forecasts": n_combos,
            "council_votes": n_votes, "weight_changes": changes}
