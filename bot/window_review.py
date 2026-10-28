"""A running read of every 15-minute chart from 20 seconds before it starts to
90 seconds in, the last 20 seconds when it ends, and a verdict when it settles.

For each coin's new window the live bot reads (every few seconds from 20 seconds
before the start until the bet is allowed at 90 seconds; the latest read is
kept and the trail of earlier ones with it) everything it can see: spot and the cross-exchange composite, the
Kalshi price and book, every signal, the chart indicators, the token,
liquidity and volume facts, and its own blended forecast, plus a one-line
plain-English read. When Kalshi settles the window, the same record gets what
really happened: who was closer (the bot or Kalshi), which signal was best and
worst, and a short note.

Because the read is taken at the same point of every window, whether or not
the bot bets, it is an unbiased sample of the bot's forecasting, unlike the
bets themselves. The same moment is also saved as a labelled training row
(crypto_brain.research logs it), so each new chart teaches the next model.
"""
import json
import math
import time

from . import brain

COIN_NAME = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "BNB"}
KEEP_FEATURES = ("rsi_1m", "rsi_5m", "rsi_15m", "adx_1m", "bb_pctb", "macd_hist", "ema_9_21", "vwap_dist",
                 "ret_5m", "ret_15m", "vol_ratio_15_60", "dist_sigma", "vol_spike_5m", "model_prob")
SCHEMA = """CREATE TABLE IF NOT EXISTS window_log (
    ticker TEXT PRIMARY KEY, coin TEXT, series TEXT, open_ts INTEGER, close_ts INTEGER, read_ts INTEGER,
    mins_left REAL, strike REAL, spot REAL, kalshi_mid REAL, kalshi_spread REAL,
    bot_prob REAL, bot_conf REAL, signals TEXT, snapshot TEXT, summary TEXT,
    outcome INTEGER, bot_brier REAL, kalshi_brier REAL, best_signal TEXT, worst_signal TEXT,
    settled_ts INTEGER, note TEXT, closing TEXT, closing_text TEXT, hindsight TEXT)"""
NEW_COLUMNS = ("closing TEXT", "closing_text TEXT", "hindsight TEXT")


def ensure_table(store):
    store.x(SCHEMA)
    have = {r["name"] for r in store.q("PRAGMA table_info(window_log)")}
    for col in NEW_COLUMNS:
        if col.split()[0] not in have:      # older databases: add the review columns in place
            store.x(f"ALTER TABLE window_log ADD COLUMN {col}")


def _r(x, n=3):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def describe(coin, feats, kalshi_mid, bot_prob, spot=None, strike=None):
    """One plain-English line: trend, momentum, volume, liquidity, and the two prices."""
    f = feats or {}
    bits = []
    adx, ema = f.get("adx_1m"), f.get("ema_9_21")
    if adx is not None and ema is not None:
        trend = "trending " + ("up" if ema > 0 else "down") if adx >= 25 else "choppy"
        bits.append(f"{trend} (ADX {adx:.0f})")
    rsi = f.get("rsi_15m")
    if rsi:
        bits.append("overbought" if rsi >= 70 else "oversold" if rsi <= 30 else f"RSI {rsi:.0f}")
    vs = f.get("tk_vol_15m_vs_60m") or f.get("vol_spike_5m")
    if vs:
        bits.append("volume " + ("surging" if vs >= 1.5 else "thin" if vs <= 0.6 else "normal"))
    vr = f.get("vol_ratio_15_60")
    if vr:
        bits.append("volatility " + ("rising" if vr >= 1.3 else "falling" if vr <= 0.75 else "steady"))
    depth = f.get("tk_k_depth_5c_log")
    if depth:
        bits.append("Kalshi book " + ("deep" if depth >= 4 else "thin" if depth < 3 else "ok"))
    where = ""
    if spot and strike:
        gap = (spot / strike - 1) * 100
        where = f"{COIN_NAME.get(coin, coin)} {abs(gap):.2f}% {'above' if gap >= 0 else 'below'} the target. "
    call = "YES" if bot_prob >= 0.5 else "NO"
    read = ", ".join(bits)
    read = read[:1].upper() + read[1:]            # str.capitalize() would flatten ADX, RSI and Kalshi
    return (where + read + (". " if bits else "") +
            f"Bot {bot_prob:.0%} vs Kalshi {kalshi_mid:.0%}; leans {call}.").strip()


def _compose(store, ticker, info, terms, now, readings, ctx, weights, late=False, playbook=None, pre_open=None):
    """Everything the bot sees at this moment, as a window_log row. None if it can't price the market."""
    prob, conf = brain.blend(readings, weights)
    if prob is None:
        return None
    prob = brain.calibrate(prob, store, domain="crypto")
    bid, ask = ctx.get("kalshi_yes_bid"), ctx.get("kalshi_yes_ask")
    if bid is None or ask is None:
        return None
    mid = (float(bid) + float(ask)) / 2
    feats = ctx.get("features") or {}
    spot = (ctx.get("spot") or {}).get("composite")
    close_ts = int(terms["close_ts"])
    snapshot = {
        "indicators": {k: _r(feats.get(k)) for k in KEEP_FEATURES if feats.get(k) is not None},
        "token": {k: _r(v, 4) for k, v in feats.items() if k.startswith("tk_")},
        "setups": [x.get("name") for x in (ctx.get("chart_setups") or {}).get("setups", [])][:6],
        "spot": {k: _r((ctx.get("spot") or {}).get(k)) for k in
                 ("composite", "source_count", "source_divergence_bps", "vol_per_min", "book_imbalance_0.1%")},
        "context": {k: _r(v, 4) for k, v in (ctx.get("market_context") or {}).items()
                    if isinstance(v, (int, float))},
        "sigma_per_min": _r(ctx.get("vol_forecast_per_min"), 7),
        "features": {k: _r(v, 4) for k, v in feats.items() if isinstance(v, (int, float))},   # what the hindsight review replays
        "late": bool(late),
        "elapsed_s": round(900 - (close_ts - now)),
        "playbook": playbook,
        "pre_open": [{k: v for k, v in r.items() if k != "ts"} for r in (pre_open or [])][-6:],
    }
    row = {
        "ticker": ticker, "coin": info.get("coin") or _coin(info.get("series")), "series": info.get("series"),
        "open_ts": close_ts - 900, "close_ts": close_ts, "read_ts": int(now),
        "mins_left": _r((close_ts - now) / 60, 2), "strike": terms.get("strike"), "spot": spot,
        "kalshi_mid": _r(mid, 4), "kalshi_spread": _r(float(ask) - float(bid), 4),
        "bot_prob": _r(prob, 4), "bot_conf": _r(conf, 3),
        "signals": json.dumps({r.name: _r(r.prob, 4) for r in readings}),
        "snapshot": json.dumps(snapshot, default=str),
    }
    row["summary"] = describe(row["coin"], {**feats, **snapshot["indicators"]}, mid, prob, spot, terms.get("strike"))
    return row, snapshot


def record_open(store, ticker, info, terms, now, readings, ctx, weights, late=False, playbook=None, pre_open=None):
    """Write the first read of a window. Returns the new record, or None if nothing to record.
    late=True marks a first read taken at bet time because the earlier ones were missed."""
    ensure_table(store)
    if store.q("SELECT 1 FROM window_log WHERE ticker=?", (ticker,)):
        return None
    made = _compose(store, ticker, info, terms, now, readings, ctx, weights, late, playbook, pre_open)
    if not made:
        return None
    row, snap = made
    snap["reads"] = [_step(row, snap)]
    row["snapshot"] = json.dumps(snap, default=str)
    cols = list(row)
    store.x(f"INSERT OR IGNORE INTO window_log ({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            tuple(row[c] for c in cols))
    return row


def _step(row, snap):
    return {"s": snap["elapsed_s"], "prob": row["bot_prob"], "mid": row["kalshi_mid"], "spot": row["spot"]}


MAX_READS = 24


def record_read(store, ticker, info, terms, now, readings, ctx, weights, playbook=None):
    """Re-read an open window: from 20 s before it starts to the moment the bot may bet, the bot
    keeps reading and testing everything. The row always holds the latest read (the one the
    bet comes from) and the snapshot keeps the trail of how the read moved."""
    ensure_table(store)
    old = store.q("SELECT snapshot, outcome FROM window_log WHERE ticker=?", (ticker,))
    if not old or old[0]["outcome"] is not None:
        return None
    made = _compose(store, ticker, info, terms, now, readings, ctx, weights, False, playbook)
    if not made:
        return None
    row, snap = made
    try:
        first = json.loads(old[0]["snapshot"] or "{}")
    except ValueError:
        first = {}
    snap["pre_open"] = first.get("pre_open") or []
    snap["reads"] = ((first.get("reads") or []) + [_step(row, snap)])[-MAX_READS:]
    row["snapshot"] = json.dumps(snap, default=str)
    for k in ("ticker", "coin", "series", "open_ts", "close_ts"):
        row.pop(k)
    sets = ",".join(f"{c}=?" for c in row)
    store.x(f"UPDATE window_log SET {sets} WHERE ticker=? AND outcome IS NULL", (*row.values(), ticker))
    return row


def tape_stats(pts, strike):
    """The last seconds of the chart: where price went relative to the target.
    pts: [(ts, price)] oldest first. None if there is too little to say."""
    pts = sorted(p for p in pts if p[1] and p[1] > 0)
    if len(pts) < 3 or not strike:
        return None
    prices = [p for _, p in pts]
    start, end = prices[0], prices[-1]
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    vol = math.sqrt(sum((x - mean) ** 2 for x in rets) / max(len(rets) - 1, 1)) if rets else 0.0
    above = [p >= strike for p in prices]
    avg = sum(prices) / len(prices)
    return {
        "seconds": round(pts[-1][0] - pts[0][0], 1), "points": len(pts), "start": start, "end": end,
        "high": max(prices), "low": min(prices),
        "change_bps": round((end / start - 1) * 1e4, 2),
        "end_vs_strike_bps": round((end / strike - 1) * 1e4, 2),
        "avg_vs_strike_bps": round((avg / strike - 1) * 1e4, 2),
        "crossed": any(a != b for a, b in zip(above, above[1:])),
        "side": "YES" if avg >= strike else "NO",
        "vol_bps_per_s": round(vol * 1e4, 3),
    }


def closing_text(coin, st):
    move = "up" if st["change_bps"] > 0 else "down" if st["change_bps"] < 0 else "flat"
    where = "above" if st["end_vs_strike_bps"] >= 0 else "below"
    held = "crossed the target during it" if st["crossed"] else f"never crossed the target"
    return (f"Last {st['seconds']:.0f}s: {COIN_NAME.get(coin, coin)} {move} {abs(st['change_bps']):.1f} bps, "
            f"finished {abs(st['end_vs_strike_bps']):.1f} bps {where} the target, {held}; "
            f"the closing average pointed {st['side']}.")


def record_close(store, ticker, strike, tape):
    """Attach the final seconds of the chart to the window's read. Returns the stats or None."""
    ensure_table(store)
    rows = store.q("SELECT coin FROM window_log WHERE ticker=?", (ticker,))
    st = tape_stats(tape, strike)
    if not rows or not st:
        return None
    store.x("UPDATE window_log SET closing=?, closing_text=? WHERE ticker=?",
            (json.dumps(st), closing_text(rows[0]["coin"], st), ticker))
    return st


def _coin(series):
    s = str(series or "").upper()
    return next((c for c in COIN_NAME if s.startswith(f"KX{c}")), "BTC")


def settle(store, ticker, y, now=None):
    """Add what really happened to the window's opening read."""
    if type(y) is not int or y not in (0, 1):
        raise ValueError("outcome must be integer 0 or 1")
    ensure_table(store)
    rows = store.q("SELECT * FROM window_log WHERE ticker=? AND outcome IS NULL", (ticker,))
    if not rows:
        return None
    r = rows[0]
    bot_b, kal_b = (r["bot_prob"] - y) ** 2, (r["kalshi_mid"] - y) ** 2
    sig = {k: v for k, v in json.loads(r["signals"] or "{}").items() if k != "kalshi_market" and v is not None}
    best = min(sig, key=lambda k: abs(sig[k] - y)) if sig else None
    worst = max(sig, key=lambda k: abs(sig[k] - y)) if sig else None
    result = "YES" if y else "NO"
    if abs(bot_b - kal_b) < 0.005:
        verdict = "about the same as Kalshi"
    else:
        verdict = "closer than Kalshi" if bot_b < kal_b else "further than Kalshi"
    note = f"{result} won. Bot {r['bot_prob']:.0%} was {verdict} ({r['kalshi_mid']:.0%})."
    if r["closing"]:
        pointed = json.loads(r["closing"]).get("side")
        note += f" The closing tape pointed {pointed}" + (", as it settled." if pointed == result else ", but it settled the other way.")
    store.x("""UPDATE window_log SET outcome=?, bot_brier=?, kalshi_brier=?, best_signal=?, worst_signal=?,
               settled_ts=?, note=? WHERE ticker=?""",
            (y, bot_b, kal_b, best, worst, int(now or time.time()), note, ticker))
    return {"bot_brier": bot_b, "kalshi_brier": kal_b, "note": note}


def report(store, recent=24, days=14):
    """For the app: running scores over settled windows, and the latest reads."""
    ensure_table(store)
    since = int(time.time()) - days * 86400
    done = store.q("""SELECT coin, bot_prob, kalshi_mid, outcome, bot_brier, kalshi_brier FROM window_log
                      WHERE outcome IS NOT NULL AND open_ts>=?""", (since,))
    stats = {"windows": len(done)}
    if done:
        b = sum(r["bot_brier"] for r in done) / len(done)
        k = sum(r["kalshi_brier"] for r in done) / len(done)
        stats.update({"bot_brier": round(b, 4), "kalshi_brier": round(k, 4),
                      "skill": round(1 - b / k, 4) if k else None,
                      "hit_rate": round(sum(1 for r in done if (r["bot_prob"] >= 0.5) == bool(r["outcome"])) / len(done), 3),
                      "kalshi_hit_rate": round(sum(1 for r in done if (r["kalshi_mid"] >= 0.5) == bool(r["outcome"])) / len(done), 3)})
        stats["by_coin"] = {}
        for c in COIN_NAME:
            cr = [r for r in done if r["coin"] == c]
            if cr:
                cb = sum(r["bot_brier"] for r in cr) / len(cr)
                ck = sum(r["kalshi_brier"] for r in cr) / len(cr)
                stats["by_coin"][c] = {"windows": len(cr), "skill": round(1 - cb / ck, 4) if ck else None}
    last = store.q("""SELECT ticker, coin, open_ts, mins_left, strike, spot, kalshi_mid, bot_prob, summary,
                      outcome, note, best_signal, worst_signal, closing_text, hindsight FROM window_log ORDER BY open_ts DESC, coin LIMIT ?""",
                   (recent,))
    return {"stats": stats, "recent": [dict(r) for r in last]}
