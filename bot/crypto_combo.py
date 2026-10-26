"""Paper-only synthetic 3-leg crypto combo bets.

These are research simulations, not native Kalshi combo orders. Each leg must
be a fresh BTC/ETH/SOL 15-minute market with independent market evidence and
aligned settlement times. Live/demo execution is intentionally not supported.
"""
import math
import os
import time
from datetime import datetime, timezone

from . import risk
from .combo_research import _leg_forecast, conservative_leg_probability

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M")
OPEN_KEY = "crypto_combo_bets"
HEALTH_KEY = "crypto_combo_health"


def _float(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _price(value):
    value = _float(value)
    if value is None:
        return None
    if value > 1:
        value /= 100.0
    return value if 0 < value < 1 else None


def _timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _percent_env(name, default):
    value = _float(os.environ.get(name, str(default)))
    if value is None or value < 0:
        return float(default) / 100.0
    return value / 100.0 if value > 1 else value


def _latest_crypto_markets(store, now):
    rows = store.q(
        """SELECT m.* FROM market_snapshots m
           JOIN (
             SELECT ticker, MAX(ts) AS latest_ts
             FROM market_snapshots
             WHERE ts >= ? AND (series IN (?,?,?) OR ticker LIKE 'KXBTC15M-%'
                   OR ticker LIKE 'KXETH15M-%' OR ticker LIKE 'KXSOL15M-%')
             GROUP BY ticker
           ) latest ON latest.ticker=m.ticker AND latest.latest_ts=m.ts
           WHERE lower(COALESCE(m.status,'')) IN ('active','open')""",
        (now - 90, *SERIES),
    )
    selected = {}
    for series in SERIES:
        eligible = []
        for row in rows:
            ticker = str(row["ticker"] or "")
            row_series = str(row["series"] or "")
            if row_series != series and not ticker.startswith(series + "-"):
                continue
            close_ts = _timestamp(row["close_time"])
            if close_ts is None or not 120 <= close_ts - now <= 780:
                continue
            eligible.append((close_ts, row))
        if eligible:
            selected[series] = min(eligible, key=lambda item: item[0])[1]
    return selected


def scan(client, store, settings=None, now=None):
    """Create at most one fee-aware synthetic crypto combo per 15-minute window."""
    now = int(now or time.time())
    if str(getattr(settings, "mode", os.environ.get("BOT_MODE", "paper"))).lower() != "paper":
        result = {"ok": True, "enabled": True, "created": 0, "skipped": "paper_mode_only"}
        store.set_kv(HEALTH_KEY, {**result, "ts": now})
        return result

    markets = _latest_crypto_markets(store, now)
    if len(markets) != len(SERIES):
        result = {"ok": True, "enabled": True, "created": 0,
                  "skipped": "need_fresh_aligned_btc_eth_sol_markets",
                  "markets_found": sorted(markets)}
        store.set_kv(HEALTH_KEY, {**result, "ts": now})
        return result

    close_times = {series: _timestamp(row["close_time"]) for series, row in markets.items()}
    if max(close_times.values()) - min(close_times.values()) > 60:
        result = {"ok": True, "enabled": True, "created": 0,
                  "skipped": "crypto_market_close_times_not_aligned"}
        store.set_kv(HEALTH_KEY, {**result, "ts": now})
        return result

    min_leg_edge = _percent_env("CRYPTO_COMBO_MIN_LEG_EDGE", 3)
    min_combo_edge = _percent_env("CRYPTO_COMBO_MIN_EDGE", 6)
    fee_rate = float(getattr(settings, "taker_fee_rate", 0.07))
    legs, skipped = [], {}
    for series in SERIES:
        market = markets[series]
        ticker = str(market["ticker"])
        forecast = _leg_forecast(store, ticker, now, max_age_s=180)
        if not forecast or forecast["external_signal_count"] < 1:
            skipped["missing_fresh_independent_forecast"] = skipped.get("missing_fresh_independent_forecast", 0) + 1
            continue
        p_yes = float(forecast["prob_yes"])
        p_yes = conservative_leg_probability(
            p_yes, forecast["confidence"], forecast["external_signal_count"])
        yes_ask = _price(market["yes_ask"])
        yes_bid = _price(market["yes_bid"])
        no_ask = round(1.0 - yes_bid, 4) if yes_bid is not None else None
        options = []
        for side, price, probability in (
            ("yes", yes_ask, p_yes),
            ("no", no_ask, 1.0 - p_yes),
        ):
            if price is None:
                continue
            fee = risk.taker_fee(1, price, fee_rate)
            edge = probability - price - fee
            if edge >= min_leg_edge:
                options.append((edge, side, price, probability))
        if not options:
            skipped["no_leg_clears_fee_adjusted_edge"] = skipped.get("no_leg_clears_fee_adjusted_edge", 0) + 1
            continue
        edge, side, price, probability = max(options, key=lambda x: x[0])
        legs.append({
            "ticker": ticker, "event_ticker": market["event_ticker"],
            "series": series, "side": side, "price": price,
            "probability": round(probability, 6),
            "confidence": round(float(forecast["confidence"]), 4),
            "signals": forecast["signals"], "external_signal_count": forecast["external_signal_count"],
            "edge": round(edge, 6), "close_time": market["close_time"],
        })

    if len(legs) != 3:
        result = {"ok": True, "enabled": True, "created": 0,
                  "skipped": skipped or {"not_enough_eligible_crypto_legs": 1},
                  "eligible_legs": len(legs)}
        store.set_kv(HEALTH_KEY, {**result, "ts": now})
        return result

    combo_price = 1.0
    joint_probability = 1.0
    for leg in legs:
        combo_price *= leg["price"]
        joint_probability *= leg["probability"]
    # Explicit uncertainty haircut; the independence product is not treated as truth.
    haircut = 0.045
    conservative_probability = max(0.0, joint_probability - haircut)
    fee_per_contract = risk.taker_fee(1, combo_price, fee_rate)
    edge = conservative_probability - combo_price - fee_per_contract
    if edge < min_combo_edge or not 0 < combo_price < 1:
        result = {"ok": True, "enabled": True, "created": 0,
                  "skipped": {**skipped, "combo_edge_below_threshold": 1},
                  "raw_joint_probability": round(joint_probability, 6),
                  "conservative_probability": round(conservative_probability, 6),
                  "synthetic_price": round(combo_price, 6), "edge": round(edge, 6)}
        store.set_kv(HEALTH_KEY, {**result, "ts": now})
        return result

    fingerprint = "|".join(f"{leg['ticker']}:{leg['side']}" for leg in legs)
    history = store.get_kv(OPEN_KEY, []) or []
    if any(row.get("fingerprint") == fingerprint for row in history):
        result = {"ok": True, "enabled": True, "created": 0, "skipped": "already_recorded_window"}
        store.set_kv(HEALTH_KEY, {**result, "ts": now})
        return result

    max_per_bet = float(getattr(settings, "max_per_bet", os.environ.get("MAX_PER_BET", 25)) or 25)
    daily_cap = float(os.environ.get("CRYPTO_COMBO_DAILY_CAP", "15"))
    target_stake = min(float(os.environ.get("CRYPTO_COMBO_STAKE", "5")), max_per_bet, daily_cap)
    unit_cost = combo_price + fee_per_contract
    contracts = max(1, int(target_stake / unit_cost)) if unit_cost > 0 else 0
    while contracts > 1 and contracts * unit_cost > target_stake:
        contracts -= 1
    stake = round(contracts * combo_price, 4)
    fee = round(contracts * fee_per_contract, 4)
    if contracts < 1 or stake + fee > max_per_bet + 1e-8:
        result = {"ok": True, "enabled": True, "created": 0, "skipped": "stake_or_fee_limit"}
        store.set_kv(HEALTH_KEY, {**result, "ts": now})
        return result

    record = {
        "id": f"CRYPTO-COMBO-{min(close_times.values())}",
        "fingerprint": fingerprint, "ts": now, "close_ts": max(close_times.values()),
        "legs": legs, "leg_count": len(legs), "price": round(combo_price, 6),
        "predicted_yes": round(conservative_probability, 6), "edge": round(edge, 6),
        "contracts": contracts, "stake": stake, "fee": fee,
        "status": "open", "mode": "paper", "synthetic": True,
    }
    history.append(record)
    store.set_kv(OPEN_KEY, history[-1000:])
    result = {"ok": True, "enabled": True, "created": 1,
              "bet": record, "skipped": skipped, "execution": "synthetic_paper_only"}
    store.set_kv(HEALTH_KEY, {**result, "ts": now})
    return result


def grade(store, now=None):
    """Grade synthetic combos only after every component contract settles."""
    now = int(now or time.time())
    history = store.get_kv(OPEN_KEY, []) or []
    outcomes = {row["ticker"]: int(row["outcome"]) for row in store.q(
        "SELECT ticker,outcome FROM graded_markets WHERE outcome IN (0,1)"
    )}
    changed = 0
    for combo in history:
        if combo.get("status") != "open":
            continue
        if not all(leg["ticker"] in outcomes for leg in combo.get("legs", [])):
            continue
        won = all(
            (leg["side"] == "yes" and outcomes[leg["ticker"]] == 1)
            or (leg["side"] == "no" and outcomes[leg["ticker"]] == 0)
            for leg in combo["legs"]
        )
        contracts = int(combo["contracts"])
        price = float(combo["price"])
        fee = float(combo["fee"])
        combo["outcome"] = int(won)
        combo["pnl"] = round(contracts * (1.0 - price) - fee if won else -contracts * price - fee, 4)
        combo["status"] = "graded"
        combo["graded_ts"] = now
        combo["brier"] = round((float(combo["predicted_yes"]) - int(won)) ** 2, 6)
        changed += 1
    if changed:
        store.set_kv(OPEN_KEY, history[-1000:])
    summary = {
        "ts": now, "graded": changed,
        "total": len(history),
        "open": sum(row.get("status") == "open" for row in history),
        "wins": sum(row.get("status") == "graded" and row.get("outcome") == 1 for row in history),
        "pnl": round(sum(float(row.get("pnl", 0) or 0) for row in history if row.get("status") == "graded"), 4),
        "synthetic_paper_only": True,
    }
    store.set_kv("crypto_combo_summary", summary)
    return summary
