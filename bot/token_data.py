"""Token, volume and liquidity facts for the four coins, as model inputs.

All free, no key, cached, and failure-tolerant: a source that is down simply
leaves its features at 0.0, and `tk_has` records whether token data was there
at all, so the model never mistakes "unknown" for "zero".

Sources
- CoinPaprika:  market cap, 24h volume (and its change), rank, price change
                over 15m..7d. (CoinGecko rate-limits shared runners.)
- Coinbase /stats (BTC, ETH, SOL) and Kraken Ticker (BNB): 24h range and
                volume, and volume against the 30-day average.
- DefiLlama:    USDT+USDC supply change (dollars waiting to buy crypto).
- Kalshi:       the contract's own volume, open interest and live book depth.
- Exchange L2, derivatives and options readings already gathered in
  marketdata.context, and volume from the 1-minute candles.

Everything is scaled (logs, ratios, fractions) so it means the same thing for
BTC and for SOL; the trained model decides which of it helps.
"""
import math
import time

import requests

from . import marketdata

PAPRIKA = "https://api.coinpaprika.com/v1/tickers/{id}"
PAPRIKA_ID = {"BTC": "btc-bitcoin", "ETH": "eth-ethereum", "SOL": "sol-solana", "BNB": "bnb-binance-coin"}
COINBASE_STATS = "https://api.exchange.coinbase.com/products/{pair}/stats"
COINBASE_PAIR = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
STABLES = "https://stablecoins.llama.fi/stablecoins"

FEATURE_NAMES = sorted([
    "tk_has",
    # market-wide size and activity (CoinPaprika)
    "tk_mcap_log10", "tk_rank", "tk_turnover", "tk_vol24_change",
    "tk_chg_15m", "tk_chg_30m", "tk_chg_1h", "tk_chg_6h", "tk_chg_24h", "tk_chg_7d",
    # exchange day stats
    "tk_range_pos_24h", "tk_range_width_24h", "tk_relvol_30d",
    # money available to buy
    "tk_stable_chg_24h",
    # the Kalshi contract itself
    "tk_k_volume_log", "tk_k_volume_24h_log", "tk_k_oi_log", "tk_k_vol24_share",
    "tk_k_bid_size_log", "tk_k_ask_size_log", "tk_k_size_imbalance",
    "tk_k_depth_5c_log", "tk_k_depth_imbalance",
    # spot exchange liquidity (L2 book)
    "tk_ex_depth_usd_log", "tk_ex_spread_bps", "tk_ex_depth_imbalance",
    # volume from candles
    "tk_dollar_vol_15m_log", "tk_vol_15m_vs_60m", "tk_up_vol_share_15m", "tk_vol_trend_5m",
    # derivatives and options
    "tk_funding", "tk_basis", "tk_oi_change_1h", "tk_put_call", "tk_hl_funding", "tk_hl_premium",
    "tk_fear_greed",
])


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _log10(x, floor=1.0):
    v = _num(x)
    return math.log10(max(v, floor)) if v is not None else None


# --- fetchers (cached; None when the source is down) ------------------------------------
def paprika(store, coin, get=requests.get):
    pid = PAPRIKA_ID.get(coin)
    if not pid:
        return None

    def fetch():
        d = marketdata._get(PAPRIKA.format(id=pid), {"quotes": "USD"}, get=get)
        try:
            q = d["quotes"]["USD"]
            return {"rank": d.get("rank"), "market_cap": q.get("market_cap"),
                    "volume_24h": q.get("volume_24h"), "volume_24h_change": q.get("volume_24h_change_24h"),
                    "chg": {k: q.get(f"percent_change_{k}") for k in ("15m", "30m", "1h", "6h", "24h", "7d")}}
        except (TypeError, KeyError, AttributeError):
            return None
    return marketdata._cached(store, f"tk_paprika_{coin}", 10, fetch)


def day_stats(store, coin, get=requests.get):
    """24h range and volume against the 30-day average, from the exchange itself."""
    def fetch():
        pair = COINBASE_PAIR.get(coin)
        if pair:
            d = marketdata._get(COINBASE_STATS.format(pair=pair), get=get)
            try:
                v30 = _num(d.get("volume_30day"))
                return {"high": float(d["high"]), "low": float(d["low"]), "last": float(d["last"]),
                        "volume": float(d["volume"]), "avg_daily_volume": v30 / 30 if v30 else None}
            except (TypeError, KeyError, ValueError, AttributeError):
                return None
        d = marketdata._get(KRAKEN_TICKER, {"pair": marketdata.KRAKEN_PAIR.get(coin)}, get=get)
        try:
            r = next(iter(d["result"].values()))
            return {"high": float(r["h"][1]), "low": float(r["l"][1]), "last": float(r["c"][0]),
                    "volume": float(r["v"][1]), "avg_daily_volume": None}
        except (TypeError, KeyError, StopIteration, ValueError, AttributeError):
            return None
    return marketdata._cached(store, f"tk_day_{coin}", 5, fetch)


def stablecoin_change(store, get=requests.get):
    """24h change in USDT+USDC supply, as a fraction."""
    def fetch():
        d = marketdata._get(STABLES, get=get)
        try:
            now = prev = 0.0
            for a in d["peggedAssets"]:
                if a.get("symbol") in ("USDT", "USDC"):
                    now += a["circulating"]["peggedUSD"]
                    prev += a["circulatingPrevDay"]["peggedUSD"]
            return {"change": now / prev - 1} if prev else None
        except (TypeError, KeyError, AttributeError):
            return None
    v = marketdata._cached(store, "tk_stables", 60, fetch)
    return v.get("change") if isinstance(v, dict) else None


def coin_snapshot(store, coin, get=requests.get):
    """Everything slow for one coin; refreshed in the background, never in the 1 s loop."""
    return {"paprika": paprika(store, coin, get), "day": day_stats(store, coin, get),
            "stable_chg": stablecoin_change(store, get)}


# --- pure feature builders -----------------------------------------------------------
def kalshi_liquidity(raw, book=None):
    """Liquidity facts for one Kalshi contract: its own volume and open interest from
    the market record, and depth from the live book (price -> contracts)."""
    out = {}
    raw = raw or {}
    for key, src in (("volume", "volume_fp"), ("volume_24h", "volume_24h_fp"), ("open_interest", "open_interest_fp"),
                     ("bid_size", "yes_bid_size_fp"), ("ask_size", "yes_ask_size_fp")):
        out[key] = _num(raw.get(src))
    yes, no = getattr(book, "yes", None), getattr(book, "no", None)
    if yes and no:
        best_yes_bid, best_no_bid = max(yes), max(no)
        mid = (best_yes_bid + (1 - best_no_bid)) / 2
        near = lambda levels, ref: sum(q for p, q in levels.items() if p >= ref - 0.05)   # within 5 cents of the best
        yd, nd = near(yes, best_yes_bid), near(no, best_no_bid)
        out.update({"top_yes_bid_size": yes[best_yes_bid], "top_no_bid_size": no[best_no_bid],
                    "depth_5c_yes": yd, "depth_5c_no": nd, "mid": mid})
    return out


def _candle_volume(candles):
    """Volume features from 1-minute candles (oldest first)."""
    out = {}
    if len(candles) < 61:
        return out
    v = [float(c["volume"]) for c in candles[-60:]]
    last15, base60 = sum(v[-15:]), sum(v) / 4 or None
    if base60:
        out["tk_vol_15m_vs_60m"] = last15 / base60
    px = candles[-1]["close"]
    out["tk_dollar_vol_15m_log"] = _log10(last15 * px)
    up = sum(float(c["volume"]) for c in candles[-15:] if c["close"] >= c["open"])
    if last15 > 0:
        out["tk_up_vol_share_15m"] = up / last15
    prev5 = sum(v[-10:-5])
    if prev5 > 0:
        out["tk_vol_trend_5m"] = sum(v[-5:]) / prev5
    return out


def token_features(ctx, candles, kalshi=None, snapshot=None):
    """The tk_* feature dict for one decision moment. Unknowns are left out (0.0 in the model)."""
    f = {}
    snap = snapshot or (ctx or {}).get("token") or {}
    pp, day = snap.get("paprika") or {}, snap.get("day") or {}
    if pp:
        f["tk_has"] = 1.0
        f["tk_mcap_log10"] = _log10(pp.get("market_cap"))
        f["tk_rank"] = _num(pp.get("rank"))
        mc, vol = _num(pp.get("market_cap")), _num(pp.get("volume_24h"))
        if mc and vol is not None:
            f["tk_turnover"] = vol / mc
        ch = _num(pp.get("volume_24h_change"))
        f["tk_vol24_change"] = ch / 100 if ch is not None else None
        for k, v in (pp.get("chg") or {}).items():
            n = _num(v)
            f[f"tk_chg_{k}"] = n / 100 if n is not None else None
    if day:
        hi, lo, last = day.get("high"), day.get("low"), day.get("last")
        if hi and lo and last and hi > lo:
            f["tk_range_pos_24h"] = (last - lo) / (hi - lo)
            f["tk_range_width_24h"] = (hi - lo) / last
        if day.get("avg_daily_volume"):
            f["tk_relvol_30d"] = day["volume"] / day["avg_daily_volume"]
    f["tk_stable_chg_24h"] = _num(snap.get("stable_chg"))
    k = kalshi or {}
    f["tk_k_volume_log"] = _log10(k.get("volume"))
    f["tk_k_volume_24h_log"] = _log10(k.get("volume_24h"))
    f["tk_k_oi_log"] = _log10(k.get("open_interest"))
    if k.get("volume") and k.get("volume_24h") is not None:
        f["tk_k_vol24_share"] = k["volume_24h"] / k["volume"]
    bs, as_ = k.get("top_yes_bid_size", k.get("bid_size")), k.get("top_no_bid_size", k.get("ask_size"))
    f["tk_k_bid_size_log"], f["tk_k_ask_size_log"] = _log10(bs), _log10(as_)
    if bs is not None and as_ is not None and bs + as_ > 0:
        f["tk_k_size_imbalance"] = (bs - as_) / (bs + as_)
    yd, nd = k.get("depth_5c_yes"), k.get("depth_5c_no")
    if yd is not None and nd is not None and yd + nd > 0:
        f["tk_k_depth_5c_log"] = _log10(yd + nd)
        f["tk_k_depth_imbalance"] = (yd - nd) / (yd + nd)
    book = (ctx or {}).get("orderbook") or {}
    if book.get("mid"):
        f["tk_ex_depth_usd_log"] = _log10((book.get("bid_depth", 0) + book.get("ask_depth", 0)) * book["mid"])
        f["tk_ex_spread_bps"] = _num(book.get("spread_bps"))
        f["tk_ex_depth_imbalance"] = _num(book.get("depth_imbalance"))
    f.update(_candle_volume(candles or []))
    c = ctx or {}
    for key, src in (("tk_funding", "funding"), ("tk_basis", "basis"), ("tk_oi_change_1h", "oi_change_1h"),
                     ("tk_put_call", "put_call_oi"), ("tk_hl_funding", "hl_funding"),
                     ("tk_hl_premium", "hl_premium"), ("tk_fear_greed", "fear_greed")):
        f[key] = _num(c.get(src))
    return {k: v for k, v in f.items() if v is not None and k in FEATURE_NAMES}
