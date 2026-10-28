"""Backtest: replay every settled market the bot researched, using the signals
it actually recorded before the game/window started, and ask "what if?"

- What if the edge threshold were 2%, 3%, 5%, 8%?
- How does the blended model score against the Kalshi price alone?

Prices use the recorded Kalshi mid +/- half the spread (what buying would have
cost), plus fees. It ignores order-book depth, so treat it as an upper bound.
This is how you tune settings with evidence instead of guesses.
"""
import bisect
import json

from . import brain
from .risk import taker_fee
from .metrics import binary_contract_roi
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


class _AsOf:
    """Walk-forward weights and calibration tables for many cutoffs at once.

    Same results as _weights_as_of / brain.calibration_table per case, but each
    source table is read once (per-case full scans took 25+ minutes once
    graded_markets held tens of thousands of ladder strikes)."""

    def __init__(self, store, buckets=10):
        self.buckets = buckets
        self.weights = store.q("SELECT ts, signal, weight FROM weight_history ORDER BY ts")
        self.wts = [r["ts"] for r in self.weights]
        audit = store.q("""SELECT model_prob, outcome, series, settled_ts FROM decision_audit
                           WHERE brier IS NOT NULL AND model_prob IS NOT NULL
                           AND outcome IN (0,1) ORDER BY settled_ts""")
        bets = store.q("""SELECT model_prob, result, series, graded_ts FROM bets
                          WHERE status='graded' AND result IN ('yes','no')
                          AND model_prob IS NOT NULL ORDER BY graded_ts""")
        self.audit = self._prefix([(r["settled_ts"], r["series"], float(r["model_prob"]),
                                    int(r["outcome"])) for r in audit])
        self.bets = self._prefix([(r["graded_ts"], r["series"], float(r["model_prob"]),
                                   1 if r["result"] == "yes" else 0) for r in bets])

    def _prefix(self, rows):
        """{domain: (times, cumulative [n, wins] per bucket)} for bisecting by time."""
        out = {}
        for ts, series, p, y in rows:
            if ts is None:                 # unsettled rows never count as of a time
                continue
            dom = brain.domain_of(series)
            times, cum = out.setdefault(dom, ([], []))
            last = [list(c) for c in cum[-1]] if cum else [[0, 0] for _ in range(self.buckets)]
            b = min(int(p * self.buckets), self.buckets - 1)
            last[b][0] += 1
            last[b][1] += y
            times.append(ts)
            cum.append(last)
        return out

    def _table(self, prefix, domain, as_of):
        times, cum = prefix.get(domain, ([], []))
        i = bisect.bisect_right(times, as_of)
        if i == 0:
            return {}
        return {b: {"n": n, "predicted": (b + 0.5) / self.buckets, "actual": w / n}
                for b, (n, w) in enumerate(cum[i - 1]) if n}

    def calibration_table(self, domain, as_of):
        return self._table(self.audit, domain, as_of) or self._table(self.bets, domain, as_of)

    def weights_for(self, domain, cutoff_ts):
        """Call with non-decreasing cutoffs: weight updates are applied incrementally."""
        i = bisect.bisect_right(self.wts, int(cutoff_ts) * 1_000_000_000)
        if i < getattr(self, "_applied", 0):
            self._applied, self._latest = 0, {}
        latest = self.__dict__.setdefault("_latest", {})
        for row in self.weights[getattr(self, "_applied", 0):i]:
            latest[row["signal"]] = row["weight"]
        self._applied = max(i, getattr(self, "_applied", 0))
        weights = dict(brain.DEFAULT_WEIGHTS)
        weights.update({n: v for n, v in latest.items() if "@" not in n})
        suffix = "@" + domain
        weights.update({n[:-len(suffix)]: v for n, v in latest.items() if n.endswith(suffix)})
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
    asof = _AsOf(store)
    for dom in ("sports", "crypto"):
        cs = sorted((c for c in cases if c["domain"] == dom), key=lambda c: c["cutoff_ts"])
        if not cs:
            continue
        model_brier = market_brier = 0.0
        scored = []
        for c in cs:
            # Reconstruct the weights that existed before this market's decision;
            # current learned weights would leak future settlements into the backtest.
            w = asof.weights_for(dom, c["cutoff_ts"])
            p, conf = brain.blend(c["readings"], w)
            p = brain.calibrate_with_table(p, asof.calibration_table(dom, c["cutoff_ts"]))
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
            # Use the shared accounting definition used by ML and history replay.
            pnl, _, roi = binary_contract_roi(
                [{"won": b["won"], "cost": b["ask"], "fee": b["fee"]} for b in bets]
            )
            res["thresholds"][t] = {
                "bets": len(bets), "win_rate": round(sum(b["won"] for b in bets) / len(bets), 3),
                "roi": round(roi, 4) if roi is not None else None,
                "pnl_per_contract": round(pnl / len(bets), 4)}
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
