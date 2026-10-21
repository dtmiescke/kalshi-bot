"""Backtest: replay every settled market the bot researched, using the signals
it actually recorded before the game/window started, and ask "what if?"

- What if the edge threshold were 2%, 3%, 5%, 8%?
- How does the blended model score against the Kalshi price alone?

Prices use the recorded Kalshi mid +/- half the spread (what buying would have
cost), plus fees. It ignores order-book depth, so treat it as an upper bound.
This is how you tune settings with evidence instead of guesses.
"""
import json

from . import brain
from .risk import taker_fee
from .signals.base import Reading

THRESHOLDS = (0.02, 0.03, 0.05, 0.08)


def _weights_as_of(store, domain, cutoff_ts):
    """Rebuild learned signal weights using only updates available at decision time."""
    weights = dict(brain.DEFAULT_WEIGHTS)
    rows = store.q("SELECT signal, weight FROM weight_history WHERE ts<=? ORDER BY ts",
                   (int(cutoff_ts) * 1_000_000_000,))
    latest = {}
    for row in rows:
        latest[row["signal"]] = row["weight"]
    weights.update({name: value for name, value in latest.items() if "@" not in name})
    suffix = "@" + domain
    weights.update({name[:-len(suffix)]: value for name, value in latest.items()
                    if name.endswith(suffix)})
    return weights


def load_cases(store):
    cases = []
    for g in store.q("SELECT ticker, outcome FROM graded_markets"):
        start = store.get_kv(f"start_{g['ticker']}")
        if not start:
            continue
        rows = store.q("""SELECT signal, prob, confidence, detail FROM signal_readings r
                          WHERE ticker=? AND ts<=? AND ts = (
                            SELECT MAX(ts) FROM signal_readings
                            WHERE ticker=r.ticker AND signal=r.signal AND ts<=?)""",
                       (g["ticker"], brain.decision_cutoff(g["ticker"], start),
                        brain.decision_cutoff(g["ticker"], start)))
        km = next((r for r in rows if r["signal"] == "kalshi_market"), None)
        if not km:
            continue
        d = json.loads(km["detail"] or "{}")
        cutoff_ts = brain.decision_cutoff(g["ticker"], start)
        cases.append({"ticker": g["ticker"], "y": g["outcome"],
                      "domain": brain.domain_of(g["ticker"]), "cutoff_ts": cutoff_ts,
                      "mid": d.get("mid", km["prob"]), "spread": d.get("spread", 0.02),
                      "readings": [Reading(r["signal"], r["prob"], r["confidence"]) for r in rows]})
    return cases


def run(store, fee_rate=0.07, thresholds=THRESHOLDS):
    cases = load_cases(store)
    out = {"markets": len(cases), "by_domain": {}}
    for dom in ("sports", "crypto"):
        cs = [c for c in cases if c["domain"] == dom]
        if not cs:
            continue
        model_brier = market_brier = 0.0
        scored = []
        for c in cs:
            # Reconstruct the weights that existed before this market's decision;
            # current learned weights would leak future settlements into the backtest.
            w = _weights_as_of(store, dom, c["cutoff_ts"])
            p, conf = brain.blend(c["readings"], w)
            p = brain.calibrate(p, store, domain=dom, as_of_ts=c["cutoff_ts"])
            model_brier += (p - c["y"]) ** 2
            market_brier += (c["mid"] - c["y"]) ** 2
            for side, pw, ask in (("yes", p, c["mid"] + c["spread"] / 2),
                                  ("no", 1 - p, 1 - c["mid"] + c["spread"] / 2)):
                fee = taker_fee(100, ask, fee_rate) / 100
                won = (c["y"] == 1) == (side == "yes")
                scored.append({"edge": pw - ask - fee, "ask": ask, "fee": fee,
                               "won": won, "ticker": c["ticker"]})
        res = {"markets": len(cs),
               "model_brier": round(model_brier / len(cs), 4),
               "market_brier": round(market_brier / len(cs), 4), "thresholds": {}}
        for t in thresholds:
            # one bet per market: its better side, if it clears the threshold
            best = {}
            for s in scored:
                if s["edge"] >= t and s["edge"] > best.get(s["ticker"], {"edge": -9})["edge"]:
                    best[s["ticker"]] = s
            bets = list(best.values())
            if not bets:
                res["thresholds"][t] = {"bets": 0}
                continue
            staked = sum(b["ask"] for b in bets)
            pnl = sum((1 - b["ask"] if b["won"] else -b["ask"]) - b["fee"] for b in bets)
            res["thresholds"][t] = {
                "bets": len(bets), "win_rate": round(sum(b["won"] for b in bets) / len(bets), 3),
                "roi": round(pnl / staked, 4), "pnl_per_contract": round(pnl / len(bets), 4)}
        out["by_domain"][dom] = res
    return out


def text(result):
    lines = ["=== BACKTEST (replay of recorded research) ===",
             f"Settled markets with research: {result['markets']}"]
    for dom, r in result["by_domain"].items():
        verdict = "model beats market" if r["model_brier"] < r["market_brier"] else \
                  "market still better"
        lines.append(f"\n[{dom}] {r['markets']} markets | Brier model {r['model_brier']} "
                     f"vs Kalshi {r['market_brier']} -> {verdict}")
        for t, v in r["thresholds"].items():
            if not v.get("bets"):
                lines.append(f"  edge >= {t:.0%}: no bets")
            else:
                lines.append(f"  edge >= {t:.0%}: {v['bets']} bets, win {v['win_rate']:.0%}, "
                             f"ROI {v['roi']:+.1%}")
    if result["markets"] < 200:
        lines.append("\nUnder ~200 markets: treat these numbers as noise.")
    return "\n".join(lines)
