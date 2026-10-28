"""After every window settles: which setup would have worked?

Each coin's window is read from 20 seconds before it starts to 90 seconds in (bot/window_review.py). When Kalshi
settles it, this module goes back to that same read and runs the whole toolbox
over it: every rule in RULES says YES, NO or "no opinion" from the chart
indicators, token/liquidity/volume facts and the Kalshi price as they stood at
the read, and we score each against what really happened, in dollars per
contract at Kalshi's price after fees. It also writes a plain-English review of
the setup the bot itself used (its bet, if any, and the signals behind it).

One window proves nothing: with 18 rules, one will always have "worked". So the
results are filed by market condition (trend strength x volatility trend) and
only a rule that stays profitable after fees across many windows in the same
condition, with a strict statistical margin (99% one-sided), becomes part of the
PLAYBOOK. At the next read the bot looks up the current condition and
the validated rules that fire now become one more signal ("crypto_playbook"),
which, like every signal, starts at a small weight and keeps it only if it keeps
beating Kalshi on settled markets (brain.learn).
"""
import json
import math
import time

from . import risk
from .signals.base import Reading, clamp

LABELS = {
    "formula": "Price formula", "value_vs_kalshi": "Formula vs Kalshi", "trend_follow": "Trend follow",
    "momentum_5m": "5-min momentum", "momentum_15m": "15-min momentum", "macd": "MACD",
    "rsi_fade": "RSI fade", "bollinger_fade": "Bollinger fade", "bollinger_breakout": "Bollinger breakout",
    "vwap_side": "VWAP side", "volume_confirms": "Volume confirms", "obv_flow": "Volume flow (OBV)",
    "strike_side": "Side of target", "strike_far": "Far from target", "back_favorite": "Back the favorite",
    "book_pressure": "Exchange book pressure", "kalshi_book_pressure": "Kalshi book pressure",
    "funding_fade": "Funding fade", "yes_premium": "Fade the YES premium",
    # a chart trader's reads (bot/expert.py)
    "structure_follow": "Market structure", "liquidity_sweep": "Liquidity sweep",
    "squeeze_breakout": "Squeeze breakout", "rsi_divergence": "RSI divergence",
    "absorption": "Volume absorption", "us_open_momentum": "US-open momentum", "range_fade": "Range-edge fade",
}


def _sgn(x, thr=0.0):
    if x is None:
        return 0
    return 1 if x > thr else -1 if x < -thr else 0


def _g(f, k, default=None):
    v = f.get(k)
    return default if v is None else v


def _mid(f):
    lg = f.get("kalshi_logit")
    return None if lg is None else 1 / (1 + math.exp(-lg))


RULES = {
    "formula": lambda f: _sgn(_g(f, "model_prob", .5) - .5),
    "value_vs_kalshi": lambda f: _sgn(f.get("model_minus_kalshi"), 0.04),
    "trend_follow": lambda f: _sgn(f.get("ema_9_21")) if _g(f, "adx_1m", 0) >= 25 else 0,
    "momentum_5m": lambda f: _sgn(f.get("ret_5m"), 0.5),
    "momentum_15m": lambda f: _sgn(f.get("ret_15m"), 0.5),
    "macd": lambda f: _sgn(f.get("macd_hist")),
    "rsi_fade": lambda f: -1 if _g(f, "rsi_15m", 50) >= 70 else 1 if _g(f, "rsi_15m", 50) <= 30 else 0,
    "bollinger_fade": lambda f: -1 if _g(f, "bb_pctb", .5) >= .95 else 1 if _g(f, "bb_pctb", .5) <= .05 else 0,
    "bollinger_breakout": lambda f: ((1 if _g(f, "bb_pctb", .5) >= .8 else -1 if _g(f, "bb_pctb", .5) <= .2 else 0)
                                     if _g(f, "adx_1m", 0) >= 25 else 0),
    "vwap_side": lambda f: _sgn(f.get("vwap_dist")),
    "volume_confirms": lambda f: _sgn(f.get("ret_5m")) if _g(f, "vol_spike_5m", 1) >= 1.5 else 0,
    "obv_flow": lambda f: _sgn(f.get("obv_slope_15m"), 0.1),
    "strike_side": lambda f: _sgn(f.get("dist_sigma")),
    "strike_far": lambda f: _sgn(f.get("dist_sigma")) if abs(_g(f, "dist_sigma", 0)) >= 1 else 0,
    "back_favorite": lambda f: (1 if (_mid(f) or .5) >= .8 else -1 if (_mid(f) or .5) <= .2 else 0),
    # live-only inputs (token / liquidity facts): no opinion when the read lacks them
    "book_pressure": lambda f: _sgn(f.get("tk_ex_depth_imbalance"), 0.15),
    "kalshi_book_pressure": lambda f: _sgn(f.get("tk_k_depth_imbalance"), 0.3),
    "funding_fade": lambda f: -_sgn(f.get("tk_funding"), 2e-4),
    # On 1,041 settled windows Kalshi's YES price in the 30-80 cent range was 8 points too high (YES won
    # less often than priced), +4.9 cents per contract after fees buying NO, z = 2.4. Found by looking,
    # so it is a candidate: the playbook treats it like any other rule and only trusts it once it holds up.
    "yes_premium": lambda f: -1 if f.get("kalshi_logit") is not None and 0.30 <= _mid(f) <= 0.80 else 0,
    # what a discretionary crypto trader would read off the same chart; each must earn its place in the playbook
    "structure_follow": lambda f: _sgn(f.get("ex_structure"), 0.5),
    "liquidity_sweep": lambda f: _sgn(f.get("ex_sweep"), 0.5),
    "squeeze_breakout": lambda f: (_sgn(f.get("ret_5m"), 0.5)
                                   if _g(f, "ex_squeeze_pct", 1) <= 0.2 and _g(f, "vol_spike_5m", 1) >= 1.3 else 0),
    "rsi_divergence": lambda f: _sgn(f.get("ex_rsi_div"), 0.5),
    "absorption": lambda f: _sgn(f.get("ex_absorption"), 0.5),
    "us_open_momentum": lambda f: _sgn(f.get("ret_15m"), 0.5) if _g(f, "ex_us_open", 0) >= 1 else 0,
    "range_fade": lambda f: (-1 if _g(f, "ex_range_pos", .5) >= .9 and _g(f, "ex_res_sigma", 9) <= .5 else
                             1 if _g(f, "ex_range_pos", .5) <= .1 and _g(f, "ex_sup_sigma", 9) <= .5 else 0),
}


def regime(f):
    """Market condition at the read: trend strength x volatility trend."""
    adx, vr = _g(f, "adx_1m", 0), _g(f, "vol_ratio_15_60", 1)
    t = "trend" if adx >= 25 else "range" if adx < 20 else "mixed"
    v = "volup" if vr >= 1.25 else "voldown" if vr <= 0.8 else "volflat"
    return f"{t}/{v}"


def votes(f):
    return {name: int(fn(f)) for name, fn in RULES.items()}


def follow_pnl(vote, mid, spread, y, fee_rate=0.07):
    """Dollars per $1 contract from following `vote` at Kalshi's price, after fees."""
    spread = spread or 0.0
    cost = min((mid + spread / 2) if vote > 0 else (1 - mid + spread / 2), 0.99)
    fee = risk.taker_fee(100, cost, fee_rate) / 100
    won = (vote > 0) == (y == 1)
    return (1 - cost - fee) if won else (-cost - fee)


SCHEMA = """CREATE TABLE IF NOT EXISTS hindsight_rules (
    ticker TEXT, rule TEXT, coin TEXT, ts INTEGER, regime TEXT, vote INTEGER,
    correct INTEGER, pnl REAL, PRIMARY KEY (ticker, rule))"""


def ensure_table(store):
    store.x(SCHEMA)


def record(store, ticker, coin, ts, feats, y):
    """Score every rule on one settled window's read. Returns {rule: {vote, correct, pnl}}."""
    if type(y) is not int or y not in (0, 1) or not feats or feats.get("kalshi_logit") is None:
        return {}
    ensure_table(store)
    mid, spread, reg = _mid(feats), feats.get("kalshi_spread") or 0.0, regime(feats)
    out = {}
    for rule, vote in votes(feats).items():
        if vote == 0:
            continue
        r = {"vote": vote, "correct": int((vote > 0) == (y == 1)), "pnl": follow_pnl(vote, mid, spread, y)}
        out[rule] = r
        store.x("INSERT OR IGNORE INTO hindsight_rules VALUES(?,?,?,?,?,?,?,?)",
                (ticker, rule, coin, int(ts), reg, vote, r["correct"], r["pnl"]))
    return out


def review_text(row, bet, results):
    """Plain-English review: the setup the bot used, and what would have worked."""
    y = row["outcome"]
    won = "YES" if y else "NO"
    if bet:
        lean = bet["side"].upper()
        signals = json.loads(row["signals"] or "{}")
        yes_side = lean == "YES"
        with_bet = [LABELS.get(k, k.replace("_", " ")) for k, p in signals.items()
                    if k != "kalshi_market" and p is not None and ((p > .55) if yes_side else (p < .45))]
        pnl = bet.get("pnl")
        result = "no result yet" if pnl is None else ("won" if pnl > 0 else "lost") + f" {pnl:+.2f}"
        setup = f"Bot bet {lean} at {round((bet.get('price') or 0) * 100)}c and {result}"
        setup += f", backed by {', '.join(with_bet[:4])}." if with_bet else "."
    else:
        setup = f"Bot did not bet; it read {row['bot_prob']:.0%} against Kalshi's {row['kalshi_mid']:.0%}."
    worked = sorted((r for r in results.items() if r[1]["correct"]), key=lambda kv: -kv[1]["pnl"])
    failed = [k for k, r in results.items() if not r["correct"]]
    side = lambda r: "YES" if r["vote"] > 0 else "NO"
    if worked:
        would = "Would have worked: " + ", ".join(f"{LABELS.get(k, k)} ({side(r)})" for k, r in worked[:5]) + "."
    else:
        would = f"No rule in the toolbox called {won}."
    if failed:
        would += " Would not have: " + ", ".join(LABELS.get(k, k) for k in failed[:4]) + "."
    return f"{won} won. {setup} {would}"


def review_window(store, ticker, y):
    """Run at settlement: score the toolbox on this window's read and write the review."""
    from . import window_review
    window_review.ensure_table(store)
    rows = store.q("SELECT * FROM window_log WHERE ticker=?", (ticker,))
    if not rows:
        return None
    row = dict(rows[0])
    row["outcome"] = y
    snap = json.loads(row.get("snapshot") or "{}")
    results = record(store, ticker, row["coin"], row["read_ts"], snap.get("features") or {}, y)
    bets = store.q("SELECT side, price, contracts, pnl FROM bets WHERE ticker=? ORDER BY ts LIMIT 1", (ticker,))
    text = review_text(row, dict(bets[0]) if bets else None, results)
    store.x("UPDATE window_log SET hindsight=? WHERE ticker=?", (text, ticker))
    return text


# --- the playbook ------------------------------------------------------------------------
MIN_WINDOWS = 50       # per rule and condition
Z = 2.33               # 99% one-sided: with ~180 rule x condition tests, a loose bar would validate luck


def family_z(tests, alpha=0.05):
    """One-sided z that keeps the chance of ANY false validation in a family of `tests` at alpha."""
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - alpha / max(int(tests), 1))


def playbook(store, min_n=MIN_WINDOWS, z=None):
    """{"rules": {"regime|rule": {n, hit, mean, lcb, validated}}, "windows": N, "ts": t}.
    Conditions are the market regimes plus "all". With dozens of rules and conditions tested, a fixed
    bar would validate luck, so the bar rises with the number of tests (z=None); pass z to fix it."""
    ensure_table(store)
    rows = store.q("""SELECT regime, rule, COUNT(*) n, AVG(pnl) mean, AVG(pnl * pnl) m2, AVG(correct) hit
                      FROM hindsight_rules GROUP BY regime, rule""")
    pooled = {}
    out = {}

    zs = {"all": z if z is not None else max(Z, family_z(len(RULES))),
          "regime": z if z is not None else max(Z, family_z(len(rows)))}

    def add(key, n, mean, m2, hit):
        var = max(m2 - mean * mean, 0.0)
        zz = zs["all"] if key.startswith("all|") else zs["regime"]
        lcb = mean - zz * math.sqrt(var / n) if n else None
        out[key] = {"n": n, "hit": round(hit, 4), "mean": round(mean, 4), "lcb": round(lcb, 4),
                    "validated": bool(n >= min_n and lcb > 0)}
    for r in rows:
        add(f"{r['regime']}|{r['rule']}", r["n"], r["mean"], r["m2"], r["hit"])
        p = pooled.setdefault(r["rule"], [0, 0.0, 0.0, 0.0])
        p[0] += r["n"]; p[1] += r["mean"] * r["n"]; p[2] += r["m2"] * r["n"]; p[3] += r["hit"] * r["n"]
    for rule, (n, s1, s2, sh) in pooled.items():
        add(f"all|{rule}", n, s1 / n, s2 / n, sh / n)
    windows = store.q("SELECT COUNT(DISTINCT ticker) c FROM hindsight_rules")[0]["c"]
    return {"rules": out, "windows": windows, "ts": int(time.time())}


def backfill(store, max_windows=5000):
    """Score the toolbox on settled windows that were stored as training rows but never reviewed
    (replayed history, and live windows from before the review existed), at the earliest allowed
    decision of each window, like the 90-second bet. Returns how many windows were added."""
    from . import ml
    ensure_table(store)
    ml.ensure_table(store)
    bad = ml.untrusted_sources(store)
    done = {r["ticker"] for r in store.q("SELECT DISTINCT ticker FROM hindsight_rules")}
    best = {}
    for r in store.q("SELECT ticker, ts, coin, features, y, source FROM ml_rows WHERE y IN (0,1)"):
        if r["ticker"] in done or (r["source"], r["coin"]) in bad:
            continue
        try:
            f = json.loads(r["features"])
        except ValueError:
            continue
        left = f.get("min_left")
        if left is None or f.get("kalshi_logit") is None or not 2 <= left <= 13.55:
            continue
        if r["ticker"] not in best or left > best[r["ticker"]][0]:
            best[r["ticker"]] = (left, r["ts"], r["coin"], f, r["y"])
    added = 0
    for ticker, (_, ts, coin, f, y) in list(best.items())[:max_windows]:
        if record(store, ticker, coin, ts, f, y):
            added += 1
    return added


def split_playbook(readings):
    """(readings the forecast is blended from, the playbook's proven shift or 0.0). A validated rule's
    lower bound is already net of fees on settled windows, so it is added AFTER the forecast is shrunk
    toward Kalshi's price, not blended in as one more opinion."""
    pb = next((r for r in readings if r.name == "crypto_playbook"), None)
    rest = [r for r in readings if r.name != "crypto_playbook"]
    return rest, float((pb.detail or {}).get("shift", 0.0)) if pb else 0.0


def playbook_reading(table, feats):
    """One signal from the validated rules that fire in the current condition, or None."""
    if not table or not feats or feats.get("kalshi_logit") is None:
        return None
    reg, mid = regime(feats), _mid(feats)
    now = votes(feats)
    firing = []
    for rule, vote in now.items():
        if vote == 0:
            continue
        # prefer the evidence for this exact condition, else the rule's overall record
        e = table["rules"].get(f"{reg}|{rule}")
        if not (e and e["validated"]):
            e = table["rules"].get(f"all|{rule}")
        if e and e["validated"]:
            firing.append((e["lcb"], rule, vote))
    if not firing:
        return None
    top = sorted(firing, reverse=True)[:3]       # correlated rules aren't counted as independent votes
    shift = clamp(sum(v * lcb for lcb, _, v in top) / len(top), -0.06, 0.06)
    return Reading("crypto_playbook", clamp(mid + shift, 0.02, 0.98), min(0.6, 0.2 + 0.1 * len(top)),
                   {"regime": reg, "rules": [r for _, r, _ in top], "shift": round(shift, 4)})


def playbook_summary(store, top=8):
    """For the app: how much has been analysed, and the validated setups by condition."""
    table = playbook(store)
    validated = [(k, e) for k, e in table["rules"].items() if e["validated"]]
    validated.sort(key=lambda kv: -kv[1]["lcb"])
    return {"windows": table["windows"], "tested": len(table["rules"]),
            "validated": len(validated),
            "top": [{"regime": k.split("|")[0], "rule": k.split("|")[1],
                     "label": LABELS.get(k.split("|")[1], k.split("|")[1]), "n": e["n"],
                     "hit": e["hit"], "edge": e["lcb"]} for k, e in validated[:top]],
            "min_windows": MIN_WINDOWS}
