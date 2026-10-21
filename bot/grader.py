"""Grading: settle bets, measure closing-line value, and teach the brain.

The brain learns from EVERY market the bot researched, not just the ones it
bet — far more data, so it gets smarter much faster.
"""
import json
import logging
import time

from . import brain, ml

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
        store.x("""UPDATE bets SET status='graded', result=?, pnl=?, closing_price=?,
                   clv=?, graded_ts=? WHERE id=?""",
                (result, round(pnl, 2), close, clv, int(time.time()), b["id"]))
        done += 1
    return done


def learn_from_markets(store, client, max_fetch=150):
    """Grade every researched market that has settled, then update weights."""
    now = int(time.time())
    candidates = store.q("""SELECT DISTINCT s.ticker FROM signal_readings s
                            LEFT JOIN graded_markets g ON g.ticker = s.ticker
                            WHERE g.ticker IS NULL LIMIT ?""", (max_fetch,))
    graded = []
    for c in candidates:
        t = c["ticker"]
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
            store.grade_decision_audit(t, y, now)
        except Exception as e:
            log.warning("decision audit grading failed for %s: %s", t, e)
        ml.label(store, t, y)      # live decisions become training rows
        # the last reading of each signal before the game started
        rows = store.q("""SELECT signal, prob FROM signal_readings r
                          WHERE ticker=? AND ts<=? AND ts = (
                            SELECT MAX(ts) FROM signal_readings
                            WHERE ticker=r.ticker AND signal=r.signal AND ts<=?)""",
                       (t, brain.decision_cutoff(t, start), brain.decision_cutoff(t, start)))
        if not rows:
            store.x("INSERT OR IGNORE INTO graded_markets VALUES(?,?,?,?)",
                    (t, now, y, m.get("series_ticker") or ""))
            continue
        g = {r["signal"]: r["prob"] for r in rows}
        g["_outcome"] = y
        g["_domain"] = brain.domain_of(t)
        graded.append(g)
        for r in rows:
            store.x("INSERT INTO signal_grades VALUES(?,?,?,?,?,?,?,?)",
                    (now, None, r["signal"], r["prob"], y, (r["prob"] - y) ** 2, None,
                     m.get("event_ticker", "")))
        store.x("INSERT OR IGNORE INTO graded_markets VALUES(?,?,?,?)",
                (t, now, y, m.get("event_ticker", "")))
    changes = {}
    for dom in ("sports", "crypto"):
        batch = [g for g in graded if g["_domain"] == dom]
        if batch:
            changes.update({f"{k}@{dom}": v for k, v in brain.learn(store, batch, dom).items()})
    return len(graded), changes


def monitor_model(store, window=300):
    """LIVE DEMOTION: the model passed its tests once, but markets change. If over
    the last `window` settled crypto markets the model's live forecasts are worse
    than Kalshi's own price, retire it automatically (it's kept, not deleted)."""
    import os
    rows = store.q("""SELECT segment, signal, brier FROM signal_grades
                      WHERE signal IN ('ml_model', 'kalshi_market')
                      ORDER BY ts DESC LIMIT ?""", (window * 2,))
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


def grade(store, client):
    n_bets = settle_bets(store, client)
    n_mkts, changes = learn_from_markets(store, client)
    monitor_model(store)
    log.info("graded %d bets, %d markets, weight changes: %s", n_bets, n_mkts,
             json.dumps({k: [round(a, 3), round(b, 3)] for k, (a, b) in changes.items()}))
    return {"bets": n_bets, "markets": n_mkts, "weight_changes": changes}
