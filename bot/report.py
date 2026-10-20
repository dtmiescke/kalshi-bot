"""Performance report: is there a real edge yet?"""
import os
from collections import defaultdict

from . import brain, guard


def _pct(x):
    return f"{x * 100:.1f}%"


def build(store):
    lines = []
    bets = store.q("SELECT * FROM bets")
    graded = [b for b in bets if b["status"] == "graded"]
    open_ = [b for b in bets if b["status"] == "open"]
    lines.append("=== KALSHI SPORTS BOT REPORT ===")
    lines.append(f"Bets: {len(bets)} total | {len(open_)} open | {len(graded)} graded")
    ok, why = guard.status(store, float(os.environ.get("BANKROLL", 1000)))
    lines.append(f"Betting status: {'ACTIVE' if ok else 'PAUSED — ' + why}")
    arbs = [b for b in graded if '"arbitrage"' in (b["signals_json"] or "")]
    if arbs:
        lines.append(f"Arbitrage legs settled: {len(arbs)}, P&L ${sum(b['pnl'] or 0 for b in arbs):+.2f}")

    if graded:
        wins = sum(1 for b in graded if (b["result"] == b["side"]) or
                   (b["result"] == "sold" and (b["pnl"] or 0) > 0))
        sold = [b for b in graded if b["result"] == "sold"]
        if sold:
            lines.append(f"Sold early: {len(sold)} positions, P&L ${sum(b['pnl'] or 0 for b in sold):+.2f}")
        makers = [b for b in bets if b.get("style") == "maker"]
        if makers:
            lines.append(f"Maker (limit) fills: {len(makers)} of {len(bets)} bets")
        staked = sum(b["stake"] for b in graded)
        pnl = sum(b["pnl"] or 0 for b in graded)
        clvs = [b["clv"] for b in graded if b["clv"] is not None]
        lines.append(f"Win rate: {_pct(wins / len(graded))}  (means little on its own)")
        lines.append(f"P&L: ${pnl:+.2f} on ${staked:.2f} staked  ROI {_pct(pnl / staked) if staked else 'n/a'}")
        if clvs:
            beat = sum(1 for c in clvs if c > 0)
            lines.append(f"Avg CLV: {sum(clvs) / len(clvs) * 100:+.2f}%  | beat close "
                         f"{_pct(beat / len(clvs))}  <- the real edge test")
        if len(graded) < 200:
            lines.append(f"Sample is small ({len(graded)}). Treat results as noise until ~200+.")

        lines.append("\n-- By league --")
        seg = defaultdict(list)
        for b in graded:
            seg[b["series"]].append(b)
        for k, bs in sorted(seg.items()):
            p = sum(x["pnl"] or 0 for x in bs)
            c = [x["clv"] for x in bs if x["clv"] is not None]
            avg_c = f"{sum(c) / len(c) * 100:+.2f}%" if c else "n/a"
            lines.append(f"{k}: {len(bs)} bets, P&L ${p:+.2f}, CLV {avg_c}")

    for dom in ("sports", "crypto"):
        lines.append(f"\n-- Signal weights: {dom} (learned) --")
        for k, v in sorted(brain.get_weights(store, dom).items(), key=lambda x: -x[1]):
            lines.append(f"{k:24s} {v:.3f}")

    lines.append("\n-- Signals vs market (Brier score, lower = better) --")
    rows = store.q("""SELECT signal, COUNT(*) n, AVG(brier) b FROM signal_grades
                      GROUP BY signal ORDER BY b""")
    for r in rows:
        lines.append(f"{r['signal']:24s} n={r['n']:5d}  brier={r['b']:.4f}")
    if not rows:
        lines.append("(no settled markets graded yet)")

    runs = store.q("SELECT ts, promoted, metrics FROM model_runs ORDER BY ts DESC LIMIT 1") \
        if store.q("SELECT name FROM sqlite_master WHERE name='model_runs'") else []
    if runs:
        import json as _j
        m = _j.loads(runs[0]["metrics"])
        lines.append("\n-- Trained model (last training run) --")
        if m.get("status"):
            lines.append(f"{m['status']}: {m.get('windows')} windows so far, need {m.get('needed')}")
        else:
            lines.append(f"{'PROMOTED' if runs[0]['promoted'] else 'not promoted'} | test log loss "
                         f"model {m['test_logloss_model']} vs Kalshi {m['test_logloss_kalshi']}")
            lines.append("Top tools: " + ", ".join(f[0] for f in m.get("top_features", [])[:8]))

    cal = brain.calibration_table(store)
    if cal:
        lines.append("\n-- Calibration (said vs happened) --")
        for b, v in cal.items():
            lines.append(f"~{_pct(v['predicted'])}: n={v['n']:4d} actual {_pct(v['actual'])}")
    return "\n".join(lines)
