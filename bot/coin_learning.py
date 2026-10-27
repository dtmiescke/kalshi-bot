"""Per-coin performance and persistent, evidence-based paper-trade postmortems.

Reviews describe observable evidence and plausible contributors, not proven causality.
Learning is append-only; automatic exposure/risk increases are intentionally excluded.
"""
import json
import math

SERIES_TO_COIN = {"KXBTC15M": "BTC", "KXBNB15M": "BNB", "KXSOL15M": "SOL", "KXETH15M": "ETH"}


def coin_from_market(series=None, ticker=None):
    text = (str(series or "") + " " + str(ticker or "")).upper()
    for key, coin in SERIES_TO_COIN.items():
        if key in text:
            return coin
    for token, coin in (("BTC", "BTC"), ("BNB", "BNB"), ("SOL", "SOL"), ("ETH", "ETH")):
        if token in text:
            return coin
    return "OTHER"


def coin_performance(rows):
    """Summarize graded bets by coin; ledger P&L is net of fees."""
    buckets = {}
    for row in rows:
        coin = coin_from_market(row.get("series"), row.get("ticker"))
        if coin == "OTHER":
            continue
        b = buckets.setdefault(coin, {"coin": coin, "bets": 0, "wins": 0, "losses": 0,
                                      "net_profit": 0.0, "total_stake": 0.0, "flat": 0,
                                      "gross_wins": 0.0, "gross_losses": 0.0})
        b["bets"] += 1
        pnl = float(row.get("pnl") or 0.0)
        if pnl > 0:
            b["wins"] += 1
        elif pnl < 0:
            b["losses"] += 1
        else:
            b["flat"] += 1
        b["net_profit"] += pnl
        b["total_stake"] += float(row.get("stake") or 0.0)
        if pnl > 0:
            b["gross_wins"] += pnl
        elif pnl < 0:
            b["gross_losses"] += -pnl
    output = []
    for b in buckets.values():
        b["win_rate"] = b["wins"] / b["bets"] if b["bets"] else None
        b["roi_on_stake"] = b["net_profit"] / b["total_stake"] if b["total_stake"] else None
        b["profit_factor"] = (b["gross_wins"] / b["gross_losses"]
                              if b["gross_losses"] else (math.inf if b["gross_wins"] else None))
        for key in ("net_profit", "total_stake", "gross_wins", "gross_losses"):
            b[key] = round(b[key], 2)
        output.append(b)
    return sorted(output, key=lambda x: (x["net_profit"], x["win_rate"] or 0), reverse=True)


def _signal_names(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        return [], {}
    return sorted(str(k) for k, v in payload.items() if v not in (None, "", [], {})), payload


def build_trade_review(bet, nearby_signals=None, market_context=None):
    """Separate observed facts from hypotheses about why the trade won or lost."""
    used, payload = _signal_names(bet.get("signals_json") or bet.get("signals") or {})
    side, result = str(bet.get("side") or "").lower(), str(bet.get("result") or "").lower()
    pnl = float(bet.get("pnl") or 0.0)
    won = side == result
    observed = {
        "bet_id": bet.get("id"), "ticker": bet.get("ticker"),
        "coin": coin_from_market(bet.get("series"), bet.get("ticker")),
        "series": bet.get("series"), "mode": bet.get("mode"), "side": side,
        "result": result, "outcome": "win" if won else "loss", "pnl": round(pnl, 2),
        "price": bet.get("price"), "contracts": bet.get("contracts"), "fee": bet.get("fee"),
        "model_prob": bet.get("model_prob"), "confidence": bet.get("confidence"),
        "edge": bet.get("edge"), "clv": bet.get("clv"), "signals_used": used,
        "signal_snapshot": payload, "nearby_signal_readings": nearby_signals or [],
        "market_context": market_context or {},
    }
    facts = [f"{observed['outcome'].upper()} on {observed['coin']} {side.upper()}",
             f"settled result={result}; net P&L=${pnl:.2f}"]
    if bet.get("clv") is not None:
        facts.append(f"closing-line value={float(bet['clv']):+.4f} (positive is favorable)")
    if used:
        facts.append("recorded signal/tool keys: " + ", ".join(used))
    hypotheses = []
    if bet.get("clv") is not None and float(bet["clv"]) < 0:
        hypotheses.append("Entry price moved against the chosen side by the closing snapshot; possible timing or stale-price issue.")
    if bet.get("clv") is not None and float(bet["clv"]) > 0 and not won:
        hypotheses.append("Entry showed favorable closing-line movement but still lost; variance or probability calibration may be involved.")
    if not used:
        hypotheses.append("No structured signal snapshot was recorded; the reason for the decision is under-documented.")
    if not nearby_signals:
        hypotheses.append("No time-aligned individual signal readings were available to attribute contribution.")
    if not hypotheses:
        hypotheses.append("Compare this case with same-coin, same-setup, same-regime settled trades before assigning a cause.")
    return {
        "schema_version": 1, "observed": observed, "facts": facts,
        "hypotheses_not_proven": hypotheses,
        "lesson": "Do not claim a signal caused the result. Compare matched settled outcomes before changing weights or strategy eligibility.",
    }


def persist_trade_review(store, bet):
    """Persist one review per graded bet and an append-only learning record."""
    bet_id = bet.get("id")
    if bet_id is None or store.q("SELECT bet_id FROM trade_reviews WHERE bet_id=?", (bet_id,)):
        return False
    entry_ts = int(bet.get("ts") or 0)
    graded_ts = int(bet.get("graded_ts") or entry_ts)
    ticker = bet.get("ticker")
    signals = store.q("""SELECT signal, prob, confidence, detail, ts FROM signal_readings
        WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 80""",
        (ticker, max(0, entry_ts - 900), entry_ts + 1))
    snapshots = store.q("""SELECT ts, yes_bid, yes_ask, last_price, volume, open_interest
        FROM market_snapshots WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 5""",
        (ticker, max(0, entry_ts - 900), entry_ts + 1))
    review = build_trade_review(bet, signals, {"recent_snapshots": snapshots})
    encoded = json.dumps(review, ensure_ascii=False, sort_keys=True, default=str)
    cur = store.x("""INSERT OR IGNORE INTO trade_reviews
        (bet_id, ts, ticker, coin, outcome, pnl, review_json) VALUES(?,?,?,?,?,?,?)""",
        (int(bet_id), graded_ts, ticker,
         review["observed"]["coin"], review["observed"]["outcome"], float(bet.get("pnl") or 0.0), encoded))
    if cur.rowcount == 1:
        store.x("INSERT INTO lessons(ts, model, text) VALUES(?,?,?)",
                (graded_ts, "trade_postmortem_v1", encoded))
        return True
    return False


def coin_report(store):
    rows = store.q("""SELECT series, ticker, side, result, pnl, stake FROM bets
        WHERE mode='paper' AND status='graded' ORDER BY graded_ts, id""")
    ranked = coin_performance(rows)
    by_coin = {row["coin"]: row for row in ranked}
    # Always show BTC, BNB, SOL and ETH, even before each has settled paper bets.
    for coin in ("BTC", "BNB", "SOL", "ETH"):
        by_coin.setdefault(coin, {
            "coin": coin, "bets": 0, "wins": 0, "losses": 0, "flat": 0,
            "net_profit": 0.0, "total_stake": 0.0, "gross_wins": 0.0,
            "gross_losses": 0.0, "win_rate": None, "roi_on_stake": None,
            "profit_factor": None,
        })
    coins = sorted(by_coin.values(), key=lambda x: (x["net_profit"], x["win_rate"] or 0), reverse=True)
    return {"mode": "paper", "graded_bets": len(rows), "coins": coins}


def format_coin_report(result):
    lines = [f"PER-COIN PAPER PERFORMANCE | graded bets={result['graded_bets']}"]
    if not result["coins"]:
        return "\n".join(lines + ["No settled paper bets yet; no coin ranking available."])
    for i, row in enumerate(result["coins"], 1):
        roi = "n/a" if row["roi_on_stake"] is None else f"{row['roi_on_stake']:.1%}"
        wr = "n/a" if row["win_rate"] is None else f"{row['win_rate']:.1%}"
        pf = "n/a" if row["profit_factor"] is None else (
            "infinite (no losses)" if math.isinf(row["profit_factor"]) else f"{row['profit_factor']:.2f}")
        lines.append(f"{i}. {row['coin']}: net profit ${row['net_profit']:.2f} | "
                     f"{row['bets']} bets | win rate {wr} | ROI {roi} | profit factor {pf}")
    return "\n".join(lines)


def trade_review_report(store, limit=20):
    rows = store.q("SELECT bet_id, ts, ticker, coin, outcome, pnl, review_json FROM trade_reviews ORDER BY ts DESC, bet_id DESC LIMIT ?",
                   (int(limit),))
    for row in rows:
        try:
            row["review"] = json.loads(row.pop("review_json"))
        except (ValueError, TypeError):
            row["review"] = {}
    return rows


def learning_summary(store, min_samples=20, limit=5000):
    """Summarize historical signal-key associations without claiming causality."""
    rows = store.q("SELECT review_json FROM trade_reviews ORDER BY ts DESC LIMIT ?", (int(limit),))
    buckets = {}
    for row in rows:
        try:
            review = json.loads(row.get("review_json") or "{}")
            obs = review.get("observed") or {}
            pnl = float(obs.get("pnl") or 0.0)
            for name in set(obs.get("signals_used") or []):
                b = buckets.setdefault(name, {"signal": name, "samples": 0, "wins": 0,
                                              "losses": 0, "flat": 0, "net_profit": 0.0})
                b["samples"] += 1
                if pnl > 0:
                    b["wins"] += 1
                elif pnl < 0:
                    b["losses"] += 1
                else:
                    b["flat"] += 1
                b["net_profit"] += pnl
        except (TypeError, ValueError, AttributeError):
            continue
    result = []
    for b in buckets.values():
        b["profitable_trade_rate"] = b["wins"] / b["samples"] if b["samples"] else None
        b["net_profit"] = round(b["net_profit"], 2)
        b["evidence_level"] = "reviewable" if b["samples"] >= int(min_samples) else "low_sample"
        result.append(b)
    return sorted(result, key=lambda x: (x["net_profit"], x["samples"]), reverse=True)


def format_learning_summary(rows, min_samples=20, setups=None):
    lines = [f"TRADE-LEARNING ASSOCIATIONS | minimum sample for review={min_samples}",
             "Associations are descriptive, not proof that a tool or signal caused the outcome."]
    if not rows:
        return "\n".join(lines + ["No persisted trade reviews yet."])
    for i, row in enumerate(rows, 1):
        rate = "n/a" if row["profitable_trade_rate"] is None else f"{row['profitable_trade_rate']:.1%}"
        note = row["evidence_level"].upper()
        lines.append(f"{i}. {row['signal']}: {row['samples']} trades | profitable {rate} | "
                     f"net P&L ${row['net_profit']:.2f} | {note}")
    if setups is not None:
        lines.append("\nCHART SETUPS BY COIN")
        if not setups:
            lines.append("No named chart setups found in persisted trade snapshots yet.")
        for row in setups[:30]:
            rate = "n/a" if row["win_rate"] is None else f"{row['win_rate']:.1%}"
            lines.append(f"{row['coin']} / {row['setup']}: n={row['samples']} | win rate {rate} | "
                         f"net P&L ${row['net_profit']:.2f} | {row['evidence_level']}")
    return "\\n".join(lines)


def setup_performance(store, min_samples=20):
    """Rank observed chart setups per coin using settled trade outcomes."""
    rows = store.q("SELECT review_json FROM trade_reviews ORDER BY ts DESC LIMIT 5000")
    buckets = {}
    for row in rows:
        try:
            review = json.loads(row.get("review_json") or "{}")
            obs = review.get("observed") or {}
            coin = obs.get("coin")
            result = str(obs.get("result") or "").lower()
            side = str(obs.get("side") or "").lower()
            pnl = float(obs.get("pnl") or 0.0)
            payload = obs.get("signal_snapshot") or {}
            raw = payload.get("chart_setups") or payload.get("setups") or []
            names = set()
            if isinstance(raw, str):
                names.add(raw)
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str):
                        names.add(item)
                    elif isinstance(item, dict) and item.get("name"):
                        names.add(str(item["name"]))
            if coin in ("BTC", "BNB", "SOL", "ETH") and result in ("yes", "no"):
                for name in names:
                    b = buckets.setdefault((coin, name), {"coin": coin, "setup": name,
                        "samples": 0, "wins": 0, "net_profit": 0.0, "clv_values": []})
                    b["samples"] += 1
                    b["wins"] += int(side == result)
                    b["net_profit"] += pnl
                    clv = obs.get("clv")
                    if clv is not None:
                        b["clv_values"].append(float(clv))
        except (TypeError, ValueError, AttributeError):
            continue
    out = []
    for b in buckets.values():
        b["win_rate"] = b["wins"] / b["samples"] if b["samples"] else None
        b["net_profit"] = round(b["net_profit"], 2)
        values = b.pop("clv_values")
        b["avg_clv"] = sum(values) / len(values) if values else None
        b["evidence_level"] = "reviewable" if b["samples"] >= int(min_samples) else "low_sample"
        b["note"] = "Observed association only; use matched chronological holdouts before strategy changes."
        out.append(b)
    return sorted(out, key=lambda x: (x["net_profit"], x["samples"]), reverse=True)
