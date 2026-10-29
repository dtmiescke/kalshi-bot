"""Is there enough proof to risk real money? Seven fixed checks, all of which must pass.

These are the go-live criteria written down in advance so they cannot be bent after seeing a good week:
enough settled bets; a positive return per dollar staked (not just a positive dollar total, which a few big
wins can fake); a result that survives removing the ten best bets; positive on at least two coins; a
forecast that beats Kalshi's price out of sample; at least one strict-bar study calling something "paying";
and no drift alarm. Paper bets on 15-minute markets only. This reports status; it never changes a setting."""
import math

MIN_BETS = 1000
Z_BAR = 1.96
DROP_TOP = 10
MIN_COIN_BETS = 50
BEAT_Z = 2.0


def _mean_z(vals):
    n = len(vals)
    if n < 2:
        return None, None
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    return m, (m / (sd / math.sqrt(n)) if sd else None)


def report(store, summary=None):
    summary = summary or {}
    rows = store.q("""SELECT series, stake, pnl FROM bets WHERE mode='paper' AND status='graded' AND stake > 0
                      AND pnl IS NOT NULL AND series LIKE 'KX%15M'""")
    n = len(rows)
    rets = [float(r["pnl"]) / float(r["stake"]) for r in rows]
    mean, z = _mean_z(rets)
    pnls = sorted((float(r["pnl"]) for r in rows), reverse=True)
    trimmed = sum(pnls[DROP_TOP:]) if n > DROP_TOP else None
    by_coin = {}
    for r in rows:
        by_coin.setdefault(r["series"], []).append(float(r["pnl"]) / float(r["stake"]))
    coins_ok = [c for c, v in by_coin.items() if len(v) >= MIN_COIN_BETS and sum(v) / len(v) > 0]
    bot = ((summary.get("online") or {}).get("bot")) or {}
    bz = bot.get("z")
    drift = (bot.get("drift") or {}).get("alarm")
    paying = []
    if (summary.get("sweep") or {}).get("verdict") == "holds up":
        paying.append("settings search")
    paying += [r["label"] for r in (summary.get("entry_rules") or {}).get("rules", []) if r.get("state") == "paying"]
    paying += [s["label"] for s in (summary.get("slices") or {}).get("slices", [])
               if s.get("state") == "paying" and s.get("dim") != "all bets"]
    c = [
        ("bets", "Settled paper bets", f"{n}", f"{MIN_BETS}+", n >= MIN_BETS),
        ("per_dollar", "Return per dollar staked is positive", "–" if mean is None else f"{mean * 100:+.1f}% (z {z:.1f})" if z is not None else f"{mean * 100:+.1f}%",
         f"z at least {Z_BAR}", z is not None and z >= Z_BAR),
        ("robust", f"Still positive without the {DROP_TOP} best bets", "–" if trimmed is None else f"{trimmed:+.0f} dollars",
         "above $0", trimmed is not None and trimmed > 0),
        ("coins", "Positive on at least two coins", f"{len(coins_ok)} coin(s)", f"2+ with {MIN_COIN_BETS}+ bets", len(coins_ok) >= 2),
        ("beats", "Bot forecast beats Kalshi's price", "–" if bz is None else f"z {bz:+.1f}", f"z at least {BEAT_Z}", bz is not None and bz >= BEAT_Z),
        ("study", "A strict-bar study calls something paying", ", ".join(paying) if paying else "none", "at least one", bool(paying)),
        ("drift", "No drift alarm", "alarm" if drift else "quiet", "quiet", drift is False),
    ]
    items = [{"id": i, "label": l, "value": v, "need": need, "met": bool(ok)} for i, l, v, need, ok in c]
    met = sum(1 for x in items if x["met"])
    return {"criteria": items, "met": met, "total": len(items), "ready": met == len(items), "bets": n}
