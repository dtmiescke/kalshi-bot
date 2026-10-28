"""Replace simulation with evidence: download real settled Kalshi 15-minute
windows (free, no API key) plus real Coinbase prices, and replay the model
minute by minute exactly as live mode would have traded.

Answers, with real data:
- How often did Kalshi's price disagree with the model by X%, and who was right?
- What win rate and return would each edge threshold have produced after fees?
- The "FOMO" question: after a sharp spot move, does Kalshi overshoot
  (overpay for YES after spikes) or undershoot? Measured, not assumed.

Each replayed window is also written into the learning tables, so the brain
starts live trading already trained on history.

Kalshi splits data into live and historical tiers (GET /historical/cutoff);
this tries the live endpoints first and falls back to /historical/... .
"""
import bisect
import logging
import math
import time
from datetime import datetime, timezone

import requests

from . import brain, features, ml, vol as volmod
from .live.settlement import contract_terms, prob_above
from .risk import taker_fee
from .metrics import binary_contract_roi

log = logging.getLogger("bot")
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{pair}/candles"
# BNB has no Coinbase product; OKX history-candles has it with real volume and,
# unlike Binance (HTTP 451 from US hosts), is reachable from GitHub runners.
OKX_CANDLES = "https://www.okx.com/api/v5/market/history-candles"
SERIES_PAIR = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
               "KXBNB15M": "OKX:BNB-USDT"}
THRESHOLDS = (0.02, 0.03, 0.05, 0.08)


def _p(obj, *names):
    """Read a candle price that may be cents (int) or dollars (*_dollars)."""
    for n in names:
        if not isinstance(obj, dict):
            return None
        v = obj.get(n + "_dollars", obj.get(n))
        if v not in (None, ""):
            v = float(v)
            return v / 100 if v > 1 else v
    return None


def settled_markets(client, series, start_ts, end_ts, limit=300):
    out, seen = [], set()
    for path in ("/markets", "/historical/markets"):
        params = {"series_ticker": series, "limit": 200}
        if path == "/markets":
            params.update({"status": "settled", "min_close_ts": int(start_ts),
                           "max_close_ts": int(end_ts)})
        try:
            rows = client._paged(path, "markets", params, max_pages=10)
        except Exception as e:
            log.info("%s %s: %s", path, series, e)
            continue
        for m in rows:
            ct = contract_terms(m)
            if (m["ticker"] not in seen and m.get("result") in ("yes", "no") and ct["close_ts"]
                    and start_ts <= ct["close_ts"] <= end_ts):
                seen.add(m["ticker"])
                out.append(m)
        if out:
            # Recent settled markets are all on /markets. The archive takes no date
            # filter, so paging it here only burns rate limit (the trainer timed out).
            break
    return sorted(out, key=lambda m: m.get("close_time", ""))[-limit:]


def market_candles(client, series, ticker, start_ts, end_ts):
    params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": 1}
    for path in (f"/series/{series}/markets/{ticker}/candlesticks",
                 f"/historical/markets/{ticker}/candlesticks"):
        try:
            c = client._req("GET", path, params=params).get("candlesticks", [])
            if c:
                return c
        except Exception:
            continue
    return []


def spot_minutes(pair, start_ts, end_ts, get=requests.get):
    """{minute_ts: close} from Coinbase."""
    return {k: c["close"] for k, c in spot_candles(pair, start_ts, end_ts, get).items()}


def okx_candles(inst, start_ts, end_ts, get=requests.get):
    """{minute_ts: full 1-minute candle} from OKX, 100 per request, newest first,
    paging backwards with `after` (returns candles older than that time)."""
    out, after, start = {}, int(end_ts) * 1000 + 60_000, int(start_ts)
    for _ in range(2000):                      # hard stop: ~138 days of minutes
        try:
            r = get(OKX_CANDLES, timeout=15, params={"instId": inst, "bar": "1m",
                                                     "limit": 100, "after": after})
            r.raise_for_status()
            rows = (r.json() or {}).get("data") or []
        except Exception as e:
            log.info("okx %s: %s", inst, e)
            break
        if not rows:
            break
        for c in rows:
            ts = int(c[0]) // 1000
            if start <= ts <= int(end_ts):
                out[ts] = {"ts": ts, "open": float(c[1]), "high": float(c[2]),
                           "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
        oldest = min(int(c[0]) for c in rows)
        if oldest // 1000 <= start or oldest >= after:
            break
        after = oldest
        time.sleep(0.12)                       # OKX allows 20 requests / 2 s
    return out


def spot_candles(pair, start_ts, end_ts, get=requests.get):
    """{minute_ts: full 1-minute candle} from Coinbase, 300 candles per request
    (pairs written "OKX:<instId>" come from OKX instead)."""
    if str(pair).startswith("OKX:"):
        return okx_candles(pair[4:], start_ts, end_ts, get)
    # Whole seconds on both ends: with a fractional end_ts, t stopped at
    # int(end_ts) < end_ts and this loop never ended (the trainer's 45-minute hang).
    out, t, end = {}, int(start_ts), int(end_ts)
    while t < end:
        chunk_end = min(t + 300 * 60, end)
        try:
            r = get(COINBASE_CANDLES.format(pair=pair), timeout=15, params={
                "granularity": 60,
                "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(chunk_end, timezone.utc).isoformat()})
            r.raise_for_status()
            for c in r.json():
                out[int(c[0])] = {"ts": int(c[0]), "low": float(c[1]), "high": float(c[2]),
                                  "open": float(c[3]), "close": float(c[4]),
                                  "volume": float(c[5])}
        except Exception as e:
            log.info("coinbase %s: %s", pair, e)
        t = chunk_end
        time.sleep(0.2)
    return out


def vol_before(spot, ts, window=60):
    closes = [spot[m] for m in range(ts - window * 60, ts + 1, 60) if m in spot]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(rets) < 20:
        return None
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))


def replay_market(m, candles, spot, fee_rate, min_left=2, max_left=13, spot_list=None,
                  store=None, spot_keys=None):
    """One record per minute where entry was allowed: model vs Kalshi prices.
    With spot_list (sorted full candles) and store, also saves the full feature
    row for the trained model."""
    terms = contract_terms(m)
    y = 1 if m["result"] == "yes" else 0
    rows = []
    for c in candles:
        ts = int(c.get("end_period_ts", 0))
        secs = terms["close_ts"] - ts
        if not (min_left * 60 <= secs <= max_left * 60):
            continue
        # LOOK-AHEAD FIX: a Kalshi candle is stamped at the END of its minute, but a
        # Coinbase candle is stamped at its START. The last spot price actually known
        # at time ts is the Coinbase candle that STARTED one minute earlier.
        minute = ts - ts % 60
        known = minute - 60
        s = spot.get(known)
        vol = vol_before(spot, known)
        bid, ask = _p(c.get("yes_bid"), "close"), _p(c.get("yes_ask"), "close")
        if not (s and vol and bid is not None and ask and terms["strike"]):
            continue
        p = prob_above(s, terms["strike"], vol, secs, terms["avg_seconds"])
        past = spot.get(known - 300)
        z5 = (math.log(s / past) / (vol * math.sqrt(5))) if past else 0.0
        rows.append({"ts": ts, "secs": secs, "model": p, "bid": bid, "ask": ask,
                     "mid": (bid + ask) / 2, "y": y, "z5": z5})
        if spot_list and store is not None:
            keys = spot_keys or [x["ts"] for x in spot_list]
            i = bisect.bisect_right(keys, known)
            window = spot_list[max(0, i - 300):i]
            feats = features.build(window, terms["strike"], secs, bid, ask, now_ts=known + 60,
                                   avg_seconds=terms["avg_seconds"])
            if feats:
                ml.add_row(store, m["ticker"], ts, ml.coin_of(m["ticker"]), feats, y, "history")
    return rows


def evaluate(records_by_market, fee_rate, thresholds=THRESHOLDS):
    res = {"markets": len(records_by_market), "thresholds": {}}
    snap = [rs[len(rs) // 2] for rs in records_by_market.values() if rs]
    if snap:
        res["model_brier"] = round(sum((r["model"] - r["y"]) ** 2 for r in snap) / len(snap), 4)
        res["kalshi_brier"] = round(sum((r["mid"] - r["y"]) ** 2 for r in snap) / len(snap), 4)
    for t in thresholds:
        bets = []
        for rs in records_by_market.values():
            for r in rs:   # first minute the edge appears, like live mode
                if r["ask"] - r["bid"] > ml.MAX_SIM_SPREAD:
                    continue   # quote too wide to trust there was real size
                for side, pw, cost in (("yes", r["model"], r["ask"] + ml.SLIPPAGE),
                                       ("no", 1 - r["model"], 1 - r["bid"] + ml.SLIPPAGE)):
                    fee = taker_fee(100, cost, fee_rate) / 100
                    if 0.05 <= cost <= 0.95 and pw - cost - fee >= t:
                        won = (r["y"] == 1) == (side == "yes")
                        bets.append({"won": won, "cost": cost, "fee": fee})
                        break
                else:
                    continue
                break
        if bets:
            _, _, roi = binary_contract_roi(bets)
            res["thresholds"][t] = {"bets": len(bets),
                                    "win_rate": round(sum(b["won"] for b in bets) / len(bets), 3),
                                    "roi": round(roi, 4) if roi is not None else None}
        else:
            res["thresholds"][t] = {"bets": 0}
    # FOMO check: after big 5-minute moves, how does Kalshi compare with the model?
    for label, cond in (("after_spike_up", lambda r: r["z5"] > 2),
                        ("after_spike_down", lambda r: r["z5"] < -2)):
        rs = [r for v in records_by_market.values() for r in v if cond(r)]
        if len(rs) >= 10:
            res[label] = {"n": len(rs),
                          "kalshi_minus_model": round(sum(r["mid"] - r["model"] for r in rs) / len(rs), 4),
                          "kalshi_brier": round(sum((r["mid"] - r["y"]) ** 2 for r in rs) / len(rs), 4),
                          "model_brier": round(sum((r["model"] - r["y"]) ** 2 for r in rs) / len(rs), 4)}
    return res


def seed_learning(store, records_by_market):
    """Feed history to the brain (once per window) so live mode starts trained."""
    graded = []
    for ticker, rs in records_by_market.items():
        if not rs or store.q("SELECT 1 FROM graded_markets WHERE ticker=?", (ticker,)):
            continue
        r = rs[len(rs) // 2]
        store.add_signal(r["ts"], ticker, "kalshi_market", r["mid"], 0.9,
                         {"mid": r["mid"], "spread": r["ask"] - r["bid"], "source": "history"})
        store.add_signal(r["ts"], ticker, "crypto_model", r["model"], 0.75, {"source": "history"})
        store.set_kv(f"start_{ticker}", r["ts"] + int(r["secs"]))
        store.x("INSERT OR IGNORE INTO graded_markets VALUES(?,?,?,?)",
                (ticker, int(time.time()), r["y"], "history"))
        graded.append({"kalshi_market": r["mid"], "crypto_model": r["model"], "_outcome": r["y"]})
    return brain.learn(store, graded, "crypto") if graded else {}


def _have_history(store):
    """Tickers already replayed into training rows (skipped on later runs)."""
    try:
        return {r["ticker"] for r in store.q(
            "SELECT DISTINCT ticker FROM ml_rows WHERE source='history'")}
    except Exception:
        return set()


def run(store, client, days=3, series=None, fee_rate=0.07, max_markets=300, spot_get=requests.get,
        deadline_ts=None):
    """deadline_ts: stop downloading at this time and keep what was gathered, so
    the trainer always has time left to train (it used to time out here)."""
    end = time.time() - 600
    start = end - days * 86400
    out = {"days": days, "by_series": {}}
    have = _have_history(store)

    def late():
        return deadline_ts is not None and time.time() > float(deadline_ts)
    for ser in series or SERIES_PAIR:
        if late():
            out["stopped"] = "time limit"
            break
        markets = [m for m in settled_markets(client, ser, start, end, limit=max_markets)
                   if m["ticker"] not in have]
        if not markets:
            out["by_series"][ser] = {"markets": 0}
            continue
        full = spot_candles(SERIES_PAIR[ser], start - 7 * 3600, end, get=spot_get)
        spot = {k: c["close"] for k, c in full.items()}
        spot_list = [full[k] for k in sorted(full)]
        spot_keys = [x["ts"] for x in spot_list]
        volmod.learn_hour_profile(store, ml.coin_of(ser), spot_list)
        recs = {}
        for m in markets:
            if late():
                out["stopped"] = "time limit"
                break
            ct = contract_terms(m)["close_ts"]
            recs[m["ticker"]] = replay_market(m, market_candles(client, ser, m["ticker"], ct - 1200, ct),
                                              spot, fee_rate, spot_list=spot_list, store=store,
                                              spot_keys=spot_keys)
            time.sleep(0.1)   # stay polite with the free API
        res = evaluate(recs, fee_rate)
        res["learned"] = {k: [round(a, 3), round(b, 3)]
                          for k, (a, b) in seed_learning(store, recs).items()}
        out["by_series"][ser] = res
    return out


def vol_check(store, days=7, series=None, leads_min=(8, 5, 3), avg_seconds=60, now=None,
              spot_get=requests.get, fit=False):
    """Read-only: is the volatility forecast the right size for 15-minute windows?

    At several decision points before every 15-minute close in the last `days`,
    rebuild the forecast from Coinbase minutes with the bot's own pieces
    (vol.ewma_vol, vol.long_vol, the stored blend; option-implied vol has no
    history here, so it is left out and the blend renormalised), then score the
    move to the settlement average (final-minute mean of open/high/low/close,
    within ~1-2 bps of CF Benchmarks per the T1c check) in the model's own
    standard deviations: z = log(settle / spot) / sd, with sd from
    settlement.z_above's variance (averaging window included, no proxy term).
    A right-sized forecast gives mean z^2 ~ 1 and |z| > 2 about 4.6% of the time.
    vol_scale = sqrt(mean z^2): below 1 means the forecast is too wide (fat
    tails priced too rich, long shots look cheap). Changes no setting; the
    report goes to kv vol_check_report. With fit=True the same samples fit each
    coin's tail model (vol.learn_tail_model), kept only if it beats current
    pricing on newer held-out windows.
    """
    end = int(time.time() if now is None else now) - 600
    end -= end % 900
    start = end - int(days * 86400)
    out = {"ts": int(time.time()), "days": days, "leads_min": list(leads_min), "coins": {}}
    for ser in series or SERIES_PAIR:
        coin = ml.coin_of(ser)
        full = spot_candles(SERIES_PAIR[ser], start - 3 * 3600, end + 60, get=spot_get)
        closes = {k: c["close"] for k, c in full.items()}
        blend = dict(store.get_kv(f"vol_blend_{coin}") or volmod.DEFAULT_BLEND)
        w_sum = (blend.get("ewma", 0) + blend.get("long", 0)) or 1.0
        zs, sigmas, samples = [], [], []
        for close in range(start + 900, end + 1, 900):
            last = full.get(close - 60)                  # minute that settles the window
            if not last:
                continue
            settle = (last["open"] + last["high"] + last["low"] + last["close"]) / 4
            for lead in leads_min:
                t = close - lead * 60                    # decision time (minute boundary)
                hist = [closes[m] for m in range(t - 120 * 60, t, 60) if m in closes]
                if len(hist) < 61 or (t - 60) not in closes:
                    continue
                spot = closes[t - 60]                    # last minute fully known at t
                ew, lg = volmod.ewma_vol(hist), volmod.long_vol(hist)
                if not (ew and lg):
                    continue
                sigma = math.sqrt((blend.get("ewma", 0) * ew * ew + blend.get("long", 0) * lg * lg)
                                  / w_sum)
                tau, a = lead * 60, avg_seconds
                var = (sigma ** 2 / 60) * ((tau - a) + a / 3)
                zs.append(math.log(settle / spot) / math.sqrt(var))
                sigmas.append(sigma)
                samples.append((close, zs[-1]))
        if not zs:
            out["coins"][coin] = {"n": 0}
            continue
        n = len(zs)
        mean_z2 = sum(z * z for z in zs) / n
        sigmas.sort()
        from statistics import NormalDist
        nd = NormalDist()
        tails = {}
        for k in (1.0, 1.5, 2.0, 2.5, 3.0):
            share = sum(abs(z) > k for z in zs) / n
            # the tail shrink a that would price this: 2 * Phi(-a k) = share
            implied = (-nd.inv_cdf(share / 2) / k) if 0 < share < 1 else None
            tails[str(k)] = {"share": round(share, 4),
                             "normal": round(2 * nd.cdf(-k), 4),
                             "priced_at_0.9": round(2 * nd.cdf(-0.9 * k), 4),
                             "implied_shrink": None if implied is None else round(implied, 3)}
        out["coins"][coin] = {
            "n": n, "mean_z2": round(mean_z2, 3), "vol_scale": round(math.sqrt(mean_z2), 3),
            "tails": tails,
            "median_sigma_per_min": round(sigmas[n // 2], 7),
            "blend_used": {k: round(blend.get(k, 0) / w_sum, 3) for k in ("ewma", "long")},
        }
        if fit:
            out["coins"][coin]["tail_model"] = volmod.learn_tail_model(store, coin, samples)
    store.set_kv("vol_check_report", out)
    return out


def text(result):
    lines = [f"=== REAL HISTORY REPLAY (last {result['days']} days, Kalshi + Coinbase) ==="]
    if result.get("stopped"):
        lines.append("(download stopped at its time limit; the rest is picked up next run)")
    for ser, r in result["by_series"].items():
        if not r.get("markets"):
            lines.append(f"\n[{ser}] no settled windows found")
            continue
        lines.append(f"\n[{ser}] {r['markets']} settled windows")
        if "model_brier" in r:
            better = "model" if r["model_brier"] < r["kalshi_brier"] else "Kalshi"
            lines.append(f"  Accuracy (Brier, lower=better): model {r['model_brier']} vs "
                         f"Kalshi {r['kalshi_brier']} -> {better} was more accurate")
        for t, v in r["thresholds"].items():
            lines.append(f"  edge >= {t:.0%}: " + ("no trades" if not v["bets"] else
                         f"{v['bets']} trades, win {v['win_rate']:.0%}, return {v['roi']:+.1%}"))
        for k in ("after_spike_up", "after_spike_down"):
            if k in r:
                v = r[k]
                lines.append(f"  {k.replace('_', ' ')} (n={v['n']}): Kalshi priced YES "
                             f"{v['kalshi_minus_model']:+.1%} vs model; Brier Kalshi "
                             f"{v['kalshi_brier']} / model {v['model_brier']}")
        if r.get("learned"):
            lines.append(f"  brain updated from history: {r['learned']}")
    lines.append("\nReplay uses 1-minute candle closes, so it can't see order-book depth or"
                 " every second; treat returns as optimistic. Settlement averaging uses the"
                 " contract rules when present.")
    return "\n".join(lines)
