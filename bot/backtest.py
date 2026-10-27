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
from .metrics import binary_contract_roi
from .signals.base import Reading

THRESHOLDS = (0.02, 0.03, 0.05, 0.08)
PREDICTION_TARGET = 0.90
MIN_TARGET_SAMPLE = 100


def score_high_confidence_predictions(rows, target=PREDICTION_TARGET,
                                      min_samples=MIN_TARGET_SAMPLE):
    """Score only forecasts the model labels >=target confidence.

    rows are mappings with calibrated YES probability 'p' and settled YES/NO
    outcome 'y'. This is a diagnostic cohort, not proof of calibration and not
    a guarantee of future performance. The target is not considered validated
    until min_samples independent settled markets exist.
    """
    scored = []
    for row in rows:
        try:
            p, y = float(row["p"]), int(row["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0 and y in (0, 1)):
            continue
        confidence = max(p, 1.0 - p)
        if confidence >= target:
            scored.append((confidence, int((p >= 0.5) == bool(y))))
    n = len(scored)
    if not n:
        return {"target": target, "n": 0, "mean_confidence": None,
                "accuracy": None, "validated": False,
                "minimum_sample": min_samples}
    accuracy = sum(correct for _, correct in scored) / n
    mean_confidence = sum(conf for conf, _ in scored) / n
    return {"target": target, "n": n,
            "mean_confidence": round(mean_confidence, 4),
            "accuracy": round(accuracy, 4),
            "validated": n >= min_samples and accuracy >= target,
            "minimum_sample": min_samples}


def _paper_crypto_bet_score(store):
    """Score graded paper bets separately from all forecast opportunities."""
    rows = store.q("""SELECT side, result, pnl, fee FROM bets
                      WHERE mode='paper' AND status='graded'
                      AND result IN ('yes','no')
                      AND (ticker LIKE 'KXBTC15M%' OR ticker LIKE 'KXBNB15M%'
                           OR ticker LIKE 'KXETH15M%' OR ticker LIKE 'KXSOL15M%')""")
    n = len(rows)
    if not n:
        return {"bets": 0, "win_rate": None, "net_pnl": 0.0,
                "validated": False, "minimum_sample": MIN_TARGET_SAMPLE}
    wins = sum(1 for r in rows if str(r["side"]).lower() == str(r["result"]).lower())
    pnl = sum(float(r["pnl"] or 0.0) for r in rows)
    win_rate = wins / n
    return {"bets": n, "wins": wins, "losses": n - wins,
            "win_rate": round(win_rate, 4), "net_pnl": round(pnl, 4),
            "validated": n >= MIN_TARGET_SAMPLE and win_rate >= PREDICTION_TARGET,
            "minimum_sample": MIN_TARGET_SAMPLE}


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
    out = {"markets": len(cases), "by_domain": {},
           "target_audit": {"target": PREDICTION_TARGET,
                            "minimum_sample": MIN_TARGET_SAMPLE,
                            "high_confidence_predictions": {},
                            "paper_crypto_bets": _paper_crypto_bet_score(store)}}
    for dom in ("sports", "crypto"):
        cs = [c for c in cases if c["domain"] == dom]
        if not cs:
            continue
        model_brier = market_brier = 0.0
        scored = []
        model_predictions = []
        for c in cs:
            # Reconstruct the weights that existed before this market's decision;
            # current learned weights would leak future settlements into the backtest.
            w = _weights_as_of(store, dom, c["cutoff_ts"])
            p, conf = brain.blend(c["readings"], w)
            p = brain.calibrate(p, store, domain=dom, as_of_ts=c["cutoff_ts"])
            model_brier += (p - c["y"]) ** 2
            market_brier += (c["mid"] - c["y"]) ** 2
            model_predictions.append({"p": p, "y": c["y"]})
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
            # Use the shared accounting definition used by ML and history replay.
            pnl, _, roi = binary_contract_roi(
                [{"won": b["won"], "cost": b["ask"], "fee": b["fee"]} for b in bets]
            )
            res["thresholds"][t] = {
                "bets": len(bets), "win_rate": round(sum(b["won"] for b in bets) / len(bets), 3),
                "roi": round(roi, 4) if roi is not None else None,
                "pnl_per_contract": round(pnl / len(bets), 4)}
        out["by_domain"][dom] = res
        if dom == "crypto":
            # Keep the acceptance scorecard scoped to the user's four approved
            # 15-minute markets; never silently blend other crypto products in.
            four_coin_predictions = [
                {"p": row["p"], "y": row["y"]}
                for row, case in zip(model_predictions,
                    [case for case in cs])
                if case["ticker"].startswith(
                    ("KXBTC15M", "KXBNB15M", "KXETH15M", "KXSOL15M"))
            ]
            out["target_audit"]["high_confidence_predictions"]["crypto"] = (
                score_high_confidence_predictions(four_coin_predictions)
            )
    return out


def text(result):
    lines = ["=== BACKTEST (replay of recorded research) ===",
             f"Settled markets with research: {result['markets']}"]
    audit = result.get("target_audit", {})
    if audit:
        lines.append(f"90% target audit (minimum {audit.get('minimum_sample', MIN_TARGET_SAMPLE)} settled samples):")
        for dom, score in audit.get("high_confidence_predictions", {}).items():
            if score.get("n", 0):
                lines.append(f"  {dom} high-confidence forecasts: n={score['n']}, "
                             f"mean confidence={score['mean_confidence']:.1%}, "
                             f"accuracy={score['accuracy']:.1%}, "
                             f"validated={score['validated']}")
            else:
                lines.append(f"  {dom} high-confidence forecasts: no eligible forecasts")
        paper = audit.get("paper_crypto_bets", {})
        if paper.get("bets", 0):
            lines.append(f"  Four-coin paper bets: {paper['bets']} bets, "
                         f"win rate={paper['win_rate']:.1%}, "
                         f"net P&L=$ {paper['net_pnl']:.2f}, "
                         f"validated={paper['validated']}")
        else:
            lines.append("  Four-coin paper bets: no graded paper bets available")
        lines.append("  'validated' means only the minimum-sample/observed-hit-rate gate passed; "
                     "it does not guarantee future results.")
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
