"""Live mode for Kalshi 15-minute crypto: stream -> analytics -> model ->
shortlist -> council -> recheck -> paper/demo order.

Cadence (from the plan):
- feeds: processed continuously as messages arrive
- analytics + model: once per second
- council: only for shortlisted trades, with a per-market cooldown
- right before any order: re-read the freshest book and recompute the edge

Free-tier only: Kraken + Coinbase public streams, Deribit public DVOL,
Kalshi REST (or its WebSocket if you add your free API key), OpenRouter free models.
"""
import asyncio
import json
import logging
import math
import os
import subprocess
import shlex
import time

from .. import alerts, brain, briefing, council, crypto_brain, entry_timing, fees, guard, handoff, hindsight, ml, risk, token_data, window_review
from .. import features, marketdata, vol as volmod
from ..engine import Engine
from ..grader import grade
from ..kalshi_client import KalshiClient, price
from ..llm import LLM
from ..signals import crypto as crypto_sig
from ..signals import flow as flow_sig
from ..signals import implied_vol as iv
from ..signals.base import Reading, clamp
from . import feeds, settlement
from .analytics import SymbolAnalytics, minute_candles
from .chart_setups import analyze_chart_setups, analyze_multi_timeframe
from .price_discovery import estimate_price_discovery
from .edge_quality import edge_quality, enrich_plan
from .adaptive_learning import AdaptiveLearning, graded_setup_performance, graded_price_discovery, historical_setup_penalty
from .edge_research import classify_regime, evidence_score, graded_feature_drift
from .book import OrderBook
from .kalshi_book import KalshiBook

log = logging.getLogger("bot.live")

COINS_TO_COIN = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXBNB15M": "BNB"}
COINS = {  # Kalshi series -> (Kraken symbol, optional Coinbase product, Deribit currency)
    "KXBTC15M": ("BTC/USD", "BTC-USD", "BTC"),
    "KXETH15M": ("ETH/USD", "ETH-USD", "ETH"),
    "KXSOL15M": ("SOL/USD", "SOL-USD", None),
    # BNB is not a Coinbase spot product. Use Kraken for its live spot/book feed;
    # do not subscribe to a fabricated Coinbase product or Deribit currency.
    "KXBNB15M": ("BNB/USD", None, None),
}


class LiveRunner:
    def __init__(self, settings, store, rest=None, llm=None, dvol_fetch=None, seed=True,
                 with_context=True):
        self.s = settings
        self.store = store
        self.rest = rest or KalshiClient(settings.market_data_url(), *settings.data_key())
        self.engine = Engine(settings, store, data_client=self.rest)
        self.llm = llm if llm is not None else (
            LLM(settings.openrouter_key, store) if settings.ai_enabled() else None)
        self.dvol_fetch = dvol_fetch or iv.fetch_dvol
        self.coins = {}           # series -> SymbolAnalytics
        for series, (kr, cb, _) in COINS.items():
            self.coins[series] = SymbolAnalytics(kr, OrderBook(kr, depth=100))
        self.markets = {}         # ticker -> {"series", "event", "raw", "terms"}
        self.books = {}           # ticker -> KalshiBook
        self.ticker_updates = {}   # ticker -> latest official Kalshi ticker event
        self.ticker_update_history = {}  # ticker -> bounded receive-time YES-mid history
        self.pending = set()      # tickers under council review
        self.last_saved = {}
        self.last_adaptive_audit = {}
        self.adaptive_report_cache = {"ts": 0.0, "report": {}}
        self.last_flow_poll = {}  # REST trade-flow polling is throttled per market
        self.betting_ok, self.betting_why = True, "ok"
        self.summary = {"mode": self.s.mode, "markets": 0, "bets": 0, "skipped": {}, "alerts": []}
        self.seed = seed
        self.with_context = with_context
        self.seed_candles = {}    # series -> Coinbase 1-minute candles (OHLCV)
        self.makers = {}          # ticker -> resting maker order
        self.last_prob = {}       # ticker -> latest blended model probability (for the app)
        self.window_read = {}  # ticker -> time of its latest read
        self.tape = {}            # ticker -> {"close_ts", "strike", "pts"}: the last seconds of the chart
        self.pre_open = {}        # series -> {window start: [chart reads taken before the market opened]}
        self._pb = (0.0, None)    # (time, playbook table), refreshed every few minutes
        self._et = (0.0, None)    # (time, entry-timing table), refreshed every 10 minutes
        # A prior process may have died after Kalshi accepted a resting order.
        # Do not resume trading until that exchange state is reconciled manually.
        prior_maker = self.store.get_kv("maker_recovery")
        prior_reconciliation = self.store.get_kv("execution_reconciliation_required")
        reason = self._startup_reconciliation_reason(
            self.s.mode, prior_maker, prior_reconciliation
        )
        if reason:
            self.store.set_kv("execution_reconciliation_required", reason)
            self.betting_ok, self.betting_why = False, reason
        self.vol_samples, self.vol_done = {}, {}
        self.ctx_cache = {}       # series -> market context, refreshed off the hot loop

    @staticmethod
    def _startup_reconciliation_reason(mode, prior_maker, prior_reconciliation):
        if mode not in ("demo", "live"):
            return None
        if prior_reconciliation:
            return str(prior_reconciliation)
        if prior_maker:
            return "unresolved maker order from prior run; reconcile Kalshi open orders/positions before resuming"
        return None

    # --- setup -------------------------------------------------------------------
    def seed_history(self):
        """Fill the first hour of 1-second mids from Coinbase 1-minute candles so
        volatility and momentum work from the first second, not after an hour."""
        for series, (kr, cb, _) in COINS.items():
            # Prefer Coinbase history where available, but do not let a transient
            # Coinbase API/rate-limit failure leave the live model without the
            # 30 completed 1-minute candles required to produce crypto_model.
            # Every configured coin has a Kraken USD pair, so use it as a real
            # historical fallback (including BTC/ETH/SOL), never synthetic bars.
            candles = crypto_sig.fetch_candles(cb) if cb else []
            source = "coinbase" if len(candles) >= 30 else "none"
            if len(candles) < 30:
                fallback = crypto_sig.fetch_kraken_candles(kr)
                if len(fallback) > len(candles):
                    candles, source = fallback, "kraken_fallback"
            self.seed_candles[series] = candles
            for c in candles:
                self.coins[series].mids.append((c["ts"], c["close"]))
            log.info("crypto history seed %s: source=%s candles=%d",
                     COINS_TO_COIN[series], source, len(candles))

    def discover(self):
        """Find open 15-minute windows and retain per-series coverage diagnostics."""
        found = {}
        by_series = {}
        for series in COINS:
            stats = {"events": 0, "markets_seen": 0, "active_markets": 0,
                     "event_statuses": {}, "market_statuses": {}, "missing_tickers": 0}
            by_series[series] = stats
            try:
                events = self.rest.sports_events(series)
                stats["events"] = len(events)
            except Exception as e:
                stats["error"] = str(e)[:240]
                log.warning("discover %s: %s", series, e)
                continue
            for ev in events:
                event_status = str(ev.get("status") or "unknown")
                stats["event_statuses"][event_status] = stats["event_statuses"].get(event_status, 0) + 1
                markets = ev.get("markets") or []
                stats["markets_seen"] += len(markets)
                for m in markets:
                    status = str(m.get("status") or "unknown")
                    stats["market_statuses"][status] = stats["market_statuses"].get(status, 0) + 1
                    if status not in ("active", "open"):
                        continue
                    ticker = m.get("ticker")
                    if not ticker:
                        stats["missing_tickers"] += 1
                        continue
                    found[ticker] = {"series": series, "event": ev.get("event_ticker"),
                                     "raw": m, "terms": settlement.contract_terms(m)}
                    stats["active_markets"] += 1
        changed = set(found) != set(self.markets)
        self.markets = found
        for t in found:
            self.books.setdefault(t, KalshiBook(t))
        for t in list(self.books):
            if t not in found:
                del self.books[t]
        health = {"ts": int(time.time()), "series": by_series,
                  "events_seen": sum(v["events"] for v in by_series.values()),
                  "markets_seen": sum(v["markets_seen"] for v in by_series.values()),
                  "active_markets": len(found), "changed": changed}
        self.summary["discovery"] = health
        self.store.set_kv("live_discovery_health", health)
        log.info("live market discovery: %s", json.dumps(health, sort_keys=True))
        return changed

    def _mark_kalshi_books_stale(self):
        """Invalidate streamed books after disconnect; require REST resync."""
        for book in self.books.values():
            book.gaps += 1
            book.needs_resync = True
            book.last_update = 0.0
        self.ws_connected = False
        self.publish_feed_health()

    def publish_feed_health(self, now=None):
        """Publish shared timestamped health for dashboards and execution gates.

        Reuses the existing summary/KV channels instead of creating duplicate
        telemetry storage.
        """
        now = time.time() if now is None else float(now)
        ages = {ticker: book.age() for ticker, book in self.books.items()}
        stale = [ticker for ticker, age in ages.items() if age is None or age > 5]
        resync = [ticker for ticker, book in self.books.items() if book.needs_resync]
        ticker_updates = getattr(self, "ticker_updates", {})
        latest_ticker = max(
            (float(row.get("received_ts", 0) or 0) for row in ticker_updates.values()),
            default=0.0,
        )
        store = getattr(self, "store", None)
        reconciliation = store.get_kv("execution_reconciliation_required") if store else None
        settings = getattr(self, "s", None)
        health = {
            "ts": int(now),
            "mode": str(getattr(settings, "mode", "unknown")),
            "ws_connected": bool(getattr(self, "ws_connected", False)),
            "tracked_books": len(self.books),
            "fresh_books": len(self.books) - len(stale),
            "stale_book_count": len(stale),
            "stale_book_sample": stale[:25],
            "resync_required_count": len(resync),
            "resync_required_sample": resync[:25],
            "latest_ticker_received_ts": latest_ticker,
            "ticker_age_s": round(max(0.0, now - latest_ticker), 3) if latest_ticker else None,
            "ticker_count": len(ticker_updates),
            "execution_reconciliation_required": bool(reconciliation),
            "trading_ready": bool(getattr(self, "betting_ok", False) and not stale
                                  and not resync and not reconciliation),
        }
        summary = getattr(self, "summary", None)
        if summary is not None:
            summary["feed_health"] = health
        if store is not None:
            store.set_kv("live_feed_health", health)
        return health

    def poll_books(self, only_resync=False, only_quiet=False):
        """Refresh only eligible books; prefer bounded bulk REST snapshots.

        WebSocket-fed books are refreshed only when they need resync or are
        stale. A bulk failure falls back to the established per-market method.
        A REST snapshot never clears a sequence-gap marker unless it contains
        a valid book for that ticker.
        """
        now = time.time()
        eligible = {}
        for ticker, book in list(self.books.items()):
            age = book.age()
            if only_resync and not book.needs_resync:
                continue
            if only_quiet and not (book.needs_resync or age is None or age > 8):
                continue
            # In WS mode, don't churn fresh books. In REST-only mode the normal
            # poll remains eligible, while only_quiet/only_resync stay bounded.
            if not only_resync and not only_quiet and getattr(self, "ws_connected", False):
                if not book.needs_resync and age is not None and age <= 8:
                    continue
            eligible[ticker] = (book, book.last_update)

        if not eligible:
            return

        def apply_snapshot(ticker, book, previous_update, ob):
            if not isinstance(ob, dict) or not isinstance(ob.get("yes"), (list, dict)) or not isinstance(ob.get("no"), (list, dict)):
                raise ValueError("malformed Kalshi orderbook snapshot")
            # If an async feed updated this book while REST was in flight, keep
            # the newer feed state instead of overwriting it with an older REST read.
            if book.last_update != previous_update and book.last_update > 0:
                return
            yes = dict(ob.get("yes") or [])
            no = dict(ob.get("no") or [])
            if any(not isinstance(p, (int, float)) or not isinstance(q, (int, float))
                   or not math.isfinite(float(p)) or not math.isfinite(float(q))
                   or not 0 <= float(p) <= 1 or float(q) < 0 for p, q in list(yes.items()) + list(no.items())):
                raise ValueError("invalid price/quantity in Kalshi orderbook snapshot")
            # A REST snapshot has no WebSocket sequence number. Reset seq so
            # the next WS delta cannot be compared against an obsolete sequence.
            book.snapshot({"yes": list(yes.items()), "no": list(no.items())}, seq=None)

        bulk = getattr(self.rest, "market_orderbooks", None)
        tickers = list(eligible)
        if callable(bulk):
            # Official bulk endpoint accepts at most 100 tickers per request.
            for offset in range(0, len(tickers), 100):
                chunk = tickers[offset:offset + 100]
                try:
                    snapshots = bulk(chunk)
                    if not isinstance(snapshots, dict):
                        raise ValueError("malformed bulk orderbook response")
                    for ticker in chunk:
                        if ticker not in snapshots:
                            # Missing rows are not proof of an empty book; use
                            # the known-good single-market endpoint as fallback.
                            try:
                                book, previous_update = eligible[ticker]
                                apply_snapshot(ticker, book, previous_update, self.rest.orderbook(ticker))
                            except Exception:
                                eligible[ticker][0].gaps += 1
                            continue
                        try:
                            book, previous_update = eligible[ticker]
                            apply_snapshot(ticker, book, previous_update, snapshots[ticker])
                        except Exception:
                            eligible[ticker][0].gaps += 1
                except Exception as exc:
                    log.warning("bulk Kalshi orderbook refresh failed for %d markets: %s", len(chunk), exc)
                    for ticker in chunk:
                        book, previous_update = eligible[ticker]
                        try:
                            apply_snapshot(ticker, book, previous_update, self.rest.orderbook(ticker))
                        except Exception:
                            book.gaps += 1
            return

        for ticker, (book, previous_update) in eligible.items():
            try:
                apply_snapshot(ticker, book, previous_update, self.rest.orderbook(ticker))
            except Exception:
                book.gaps += 1

    # --- the per-second brain ------------------------------------------------------
    def readings_for(self, ticker, now):
        """Research one contract with the shared crypto brain on live candles."""
        info, book = self.markets[ticker], self.books[ticker]
        # Do not derive candidates from a book invalidated by a sequence gap,
        # disconnect, or missing trustworthy timestamp.
        if book.needs_resync or book.age() is None:
            return None, None
        a = self.coins[info["series"]]
        terms = info["terms"]
        if not (terms["strike"] and terms["close_ts"]) or not book.yes or not book.no:
            return None, None
        best_yes_bid = max(book.yes)
        best_yes_ask = 1 - max(book.no)
        candles = minute_candles(a, now, self.seed_candles.get(info["series"]))
        spot = a.composite_price(now) or a.book.mid()
        readings, rinfo = crypto_brain.research(
            self.store, ticker, info["series"], terms, candles, best_yes_bid, best_yes_ask,
            now=now, spot=spot, dvol_fetch=lambda c: None,   # cache only; refreshed in background
            with_context=False, log_row=True,
            context=self.ctx_cache.get(info["series"], {}) if self.with_context else {},
            kalshi_stats=token_data.kalshi_liquidity(info.get("raw"), book))
        if not readings:
            return None, None
        # Kalshi's public trade tape is anonymous, but reveals which outcome
        # aggressive traders are buying, trade size concentration, sweeps, and
        # whether contract prices move before the reference spot/book.
        # Keep it separate from spot-exchange aggressor flow; both are useful
        # independent evidence and neither identifies individual traders.
        kalshi_mid = (best_yes_bid + best_yes_ask) / 2
        flow_reading = self.kalshi_flow_reading(ticker, kalshi_mid, now)
        if flow_reading:
            readings.append(flow_reading)
        base = next((r.prob for r in readings if r.name == "crypto_model"), None)
        f = a.flow(now, 60)
        if base is not None and f["trades"] >= 10:
            pressure = 0.6 * f["aggressor_imbalance"] + 0.4 * a.book.imbalance(0.1)
            readings.append(Reading("spot_flow", clamp(base + 0.03 * pressure, 0.02, 0.98),
                                    clamp(abs(pressure), 0.05, 0.6),
                                    {"aggressor": f["aggressor_imbalance"],
                                     "book_imb": round(a.book.imbalance(0.1), 3)}))
        if rinfo.get("vol_parts") and spot:
            self._vol_sample(info["series"], now, spot, rinfo["vol_parts"])
        feats = rinfo.get("features") or {}
        # the playbook: setups that stayed profitable after fees in this market condition
        playbook = hindsight.playbook_reading(self.playbook_table(now), feats)
        if playbook:
            readings.append(playbook)
        spot_summary = a.summary(now)
        chart_setups = analyze_chart_setups(candles, book_imbalance=spot_summary.get("book_imbalance_0.1%"), aggressor_imbalance=spot_summary.get("flow_60s", {}).get("aggressor_imbalance"))
        multi_timeframe = analyze_multi_timeframe(candles, book_imbalance=spot_summary.get("book_imbalance_0.1%"), aggressor_imbalance=spot_summary.get("flow_60s", {}).get("aggressor_imbalance"))
        price_discovery = estimate_price_discovery(a.price_events, self.ticker_update_history.get(ticker, []))
        context = {"ticker": ticker, "secs_left": round(terms["close_ts"] - now), "contract": terms,
                   "kalshi_yes_bid": best_yes_bid, "kalshi_yes_ask": best_yes_ask,
                   "kalshi_book_age_s": round(book.age() if book.age() is not None else 99, 2),
                   "spot": spot_summary, "chart_setups": chart_setups, "multi_timeframe": multi_timeframe, "price_discovery": price_discovery, "market_context": rinfo.get("context", {}),
                   "vol_forecast_per_min": rinfo.get("sigma"), "features": feats,
                   "indicators": {k: round(v, 3) for k, v in feats.items()
                                  if k in ("rsi_1m", "rsi_5m", "rsi_15m", "macd_hist", "bb_pctb",
                                           "adx_1m", "vwap_dist", "ret_5m", "ret_15m",
                                           "vol_ratio_15_60", "dist_sigma")},
                   "signals": {r.name: round(r.prob, 3) for r in readings}}
        return readings, context

    def playbook_table(self, now):
        """Validated setups per market condition, recomputed every 5 minutes."""
        if self._pb[1] is None or now - self._pb[0] >= 300:
            try:
                self._pb = (now, hindsight.playbook(self.store))
            except Exception as exc:
                log.warning("playbook refresh failed: %s", exc)
                self._pb = (now, self._pb[1] or {"rules": {}, "windows": 0})
        return self._pb[1]

    def entry_table(self, now):
        """When in the window entries have paid (bot/entry_timing.py), recomputed every 10 minutes."""
        if self._et[1] is None or now - self._et[0] >= 600:
            try:
                self._et = (now, entry_timing.table(self.store, now))
            except Exception as exc:
                log.warning("entry timing refresh failed: %s", exc)
                self._et = (now, self._et[1])
        return self._et[1]

    def read_due(self, close_ts, now):
        """True from CRYPTO_READ_SECONDS (-20: before the window starts) until the bet is allowed (CRYPTO_ENTRY_DELAY_SECONDS)."""
        elapsed = 900 - (close_ts - now)
        return self.s.crypto_read_seconds <= elapsed < self.s.crypto_entry_delay_seconds

    def take_window_read(self, ticker, info, terms, now, readings, ctx, weights, late=False):
        """Read this window again, at most every CRYPTO_READ_EVERY_SECONDS. Failures are logged and never block trading."""
        last = self.window_read.get(ticker)
        if last is not None and now - last < self.s.crypto_read_every_seconds:
            return
        self.window_read[ticker] = now
        try:
            pb = next((r for r in readings if r.name == "crypto_playbook"), None)
            detail = pb.detail if pb else None
            pre = self.pre_open.get(info.get("series"), {}).get(int(terms["close_ts"]) - 900)
            if last is None:
                if window_review.record_open(self.store, ticker, info, terms, now, readings, ctx, weights, late=late,
                                             playbook=detail, pre_open=pre):
                    log.info("window read %s%s", ticker, " (late)" if late else "")
            else:
                window_review.record_read(self.store, ticker, info, terms, now, readings, ctx, weights, playbook=detail)
        except Exception as exc:
            log.warning("window read %s failed: %s", ticker, exc)

    def closing_tapes(self, now):
        """Keep the composite price for the last 20 seconds of every window, and when a
        window has closed, review those seconds against the target."""
        for ticker, info in self.markets.items():
            close_ts = int(info["terms"].get("close_ts") or 0)
            if close_ts and 0 < close_ts - now <= 20:
                a = self.coins[info["series"]]
                px = a.composite_price(now) or a.book.mid()
                t = self.tape.setdefault(ticker, {"close_ts": close_ts, "strike": info["terms"].get("strike"), "pts": []})
                if px and (not t["pts"] or now - t["pts"][-1][0] >= 0.5):
                    t["pts"].append((now, px))
        self.window_read = {t: v for t, v in self.window_read.items() if t in self.markets}   # finished windows drop out
        for ticker, t in list(self.tape.items()):
            if now >= t["close_ts"]:
                del self.tape[ticker]
                try:
                    if window_review.record_close(self.store, ticker, t["strike"], t["pts"]):
                        log.info("closing read %s", ticker)
                except Exception as exc:
                    log.warning("closing read %s failed: %s", ticker, exc)

    def new_window_missing(self, now, last_discover):
        """Just after a :00/:15/:30/:45 mark, look for the new windows every 3 s until every coin has
        one, so the reads start the moment Kalshi activates them (30-100 s after the mark) instead
        of up to a minute later."""
        since_open = now % 900
        if since_open > 150 or now - last_discover < 3:
            return False
        opens = now - since_open + 900              # the new windows close 15 minutes after the mark
        have = {m["series"] for m in self.markets.values() if int(m["terms"].get("close_ts") or 0) == opens}
        return any(series not in have for series in self.coins)

    def pre_open_reads(self, now):
        """Kalshi lists the next window ('initialized') with no strike or book until the mark, so
        for the 20 seconds before it the bot reads each coin's chart on its own: indicators on
        the live candles, with the current price standing in for the strike. That read is saved
        with the window once it opens."""
        lead = -self.s.crypto_read_seconds
        if lead <= 0:
            return
        opens = now - now % 900 + 900
        if not 0 < opens - now <= lead:
            self.pre_open = {sr: {t: v for t, v in w.items() if t >= opens - 900} for sr, w in self.pre_open.items()}
            return
        for series, a in self.coins.items():
            reads = self.pre_open.get(series, {}).get(opens, [])
            if reads and now - reads[-1]["ts"] < 5:
                continue
            try:
                candles = minute_candles(a, now, self.seed_candles.get(series))
                spot = a.composite_price(now) or a.book.mid()
                f = features.build(candles, spot, 900.0) if spot else None
                if f:
                    reads = self.pre_open.setdefault(series, {}).setdefault(opens, [])
                    reads.append({"ts": round(now, 1), "s": round(now - opens), "spot": window_review._r(spot, 2),
                                  "ind": {k: window_review._r(f.get(k)) for k in window_review.KEEP_FEATURES
                                          if f.get(k) is not None}})
            except Exception as exc:
                log.warning("pre-open read %s failed: %s", series, exc)

    def kalshi_flow_reading(self, ticker, kalshi_mid, now=None):
        """Build an independent signal from anonymous Kalshi prints in the last 5m."""
        now = time.time() if now is None else now
        trades = self.store.flow(ticker, int(now - 300))
        return flow_sig.trade_flow(trades, kalshi_mid)

    def _window_lock_key(self, series, close_ts):
        """One persistent entry reservation per coin and 15-minute expiry window."""
        return f"crypto_entry_window:{series}:{int(close_ts)}"

    def tick(self, now=None):
        now = now or time.time()
        for a in self.coins.values():
            a.sample(now)
        self.check_makers(now)
        if self.s.exits_enabled:
            self.check_exits(now)
        self.learn_vol(now)
        self.closing_tapes(now)
        self.pre_open_reads(now)
        weights = brain.get_weights(self.store, "crypto")
        if now - self.adaptive_report_cache["ts"] >= 60:
            try:
                setup_report = graded_setup_performance(self.store)
                discovery_report = graded_price_discovery(self.store)
                drift_report = graded_feature_drift(self.store)
                # Evidence scores rank research maturity, not probability of winning.
                for item in setup_report.values():
                    item["evidence_quality_score"] = evidence_score(
                        samples=item.get("samples", 0),
                        brier=item.get("model_brier"),
                        baseline_brier=item.get("market_brier"),
                    )
                    item["promotion_allowed"] = False  # OOS/cost/drawdown validation required
                self.adaptive_report_cache = {
                    "ts": now, "report": setup_report,
                    "price_discovery": discovery_report,
                    "feature_drift": drift_report,
                }
                self.store.set_kv("crypto_adaptive_research", {
                    "ts": int(now), "setup_performance": setup_report,
                    "price_discovery": discovery_report,
                    "feature_drift": drift_report,
                    "research_policy": {
                        "live_auto_promotion": False,
                        "requires_walk_forward": True,
                        "requires_fees_slippage_and_fill_model": True,
                        "requires_drawdown_and_drift_review": True,
                    },
                    "note": "Settled canonical forecast diagnostics only; observational research, not proof of profit."
                })
            except Exception as exc:
                log.warning("adaptive research refresh failed: %s", exc)
        candidates = {}
        for ticker, info in list(self.markets.items()):
            if ticker in self.pending or ticker in self.makers or ticker not in self.books:
                continue
            readings, ctx = self.readings_for(ticker, now)
            if not readings:
                continue
            terms = info["terms"]
            close_ts = int(terms["close_ts"] or 0)
            if close_ts <= 0:
                continue
            self.store.set_kv(f"start_{ticker}", close_ts)
            self.store.set_kv(f"settle_after_{ticker}", close_ts + 300)
            # Read and test everything from 20 s before the window starts until the bet is
            # allowed at 90 s in, every few seconds (bot/window_review.py).
            if self.read_due(close_ts, now):
                self.take_window_read(ticker, info, terms, now, readings, ctx, weights)
            # Keep one research snapshot every 30s. The grader scores only the
            # canonical pre-close decision cutoff, preventing per-second samples
            # from inflating the learning set. Research rows never place orders.
            if now - self.last_adaptive_audit.get(ticker, 0) >= 30:
                model_prob, model_conf = brain.blend(hindsight.split_playbook(readings)[0], weights)
                if model_prob is not None:
                    model_prob = brain.calibrate(model_prob, self.store, domain="crypto")
                    # every coin's line on the app's chart, not only coins that reach a trade plan
                    shown = brain.shrink_to_market(model_prob, self.books[ticker].mid(), self.store, series=info["series"])
                    self.last_prob[ticker] = {"prob": round(shown, 4),
                                              "conf": round(model_conf or 0, 3), "ts": round(now)}
                    yes_bid = ctx.get("kalshi_yes_bid")
                    yes_ask = ctx.get("kalshi_yes_ask")
                    market_mid = ((float(yes_bid) + float(yes_ask)) / 2.0
                                  if yes_bid is not None and yes_ask is not None else None)
                    spot_summary = ctx.get("spot", {})
                    regime = AdaptiveLearning.regime({
                        "realized_volatility": spot_summary.get("vol_per_min"),
                        "spread_bps": (((float(yes_ask) - float(yes_bid)) * 10000.0 / market_mid)
                                       if market_mid and yes_bid is not None and yes_ask is not None else 0),
                        "source_divergence_bps": spot_summary.get("source_divergence_bps", 0),
                        "book_imbalance": spot_summary.get("book_imbalance_0.1%", 0),
                        "trend_strength": min(1.0, max(0.0, float(ctx.get("indicators", {}).get("adx_1m", 0) or 0) / 100.0)),
                    })
                    self.store.add_decision_audit({
                        "ts": int(now), "ticker": ticker, "event_ticker": info.get("event"),
                        "series": info.get("series"), "decision": "research",
                        "reason": "chart/setup learning snapshot; no order implied",
                        "side": "yes" if model_prob >= (market_mid if market_mid is not None else 0.5) else "no",
                        "market_price": market_mid, "model_prob": model_prob,
                        "side_prob": max(model_prob, 1-model_prob),
                        "edge": (abs(model_prob-market_mid) if market_mid is not None else None),
                        "confidence": max((float(getattr(x, "confidence", 0) or 0) for x in readings), default=0),
                        "signals": {"chart_setups": [x.get("name") for x in ctx.get("chart_setups", {}).get("setups", [])],
                                    "regime": regime,
                                    "regime_tags": classify_regime({
                                        "volatility_ratio": None,  # no comparable baseline window; do not fabricate a shock score
                                        "trend_strength": float(ctx.get("indicators", {}).get("adx_1m", 0) or 0) / 100.0,
                                        "source_divergence_bps": spot_summary.get("source_divergence_bps"),
                                        "spread_bps": (((float(yes_ask) - float(yes_bid)) * 10000.0 / market_mid)
                                                       if market_mid and yes_bid is not None and yes_ask is not None else 0),
                                        "book_imbalance": spot_summary.get("book_imbalance_0.1%"),
                                    }),
                                    "indicators": ctx.get("indicators", {}),
                                    "source_divergence_bps": spot_summary.get("source_divergence_bps"),
                                    "multi_timeframe": ctx.get("multi_timeframe", {}),
                                    "price_discovery": ctx.get("price_discovery", {})},
                    })
                    self.last_adaptive_audit[ticker] = now
            if now - self.last_saved.get(ticker, 0) >= 30:
                for r in readings:
                    self.store.add_signal(int(now), ticker, r.name, r.prob, r.confidence, r.detail)
                self.last_saved[ticker] = now
            mins_left = (close_ts - now) / 60
            if not (self.s.crypto_min_minutes <= mins_left <= self.s.crypto_max_minutes):
                continue
            if not self.betting_ok or guard.league_benched(self.store, info["series"], mode=self.s.mode):
                continue
            if os.environ.get("ENTRY_TIMING_GATE", "yes") == "yes" and \
                    not entry_timing.allowed(self.entry_table(now), mins_left):
                self._skip("entry timing: entries at this point of the window have lost money")
                continue
            lock_key = self._window_lock_key(info["series"], close_ts)
            if self.store.get_kv(lock_key):
                # A trade or in-flight attempt already owns this coin/window.
                continue
            # A bet always follows a fresh read: if none was taken (a restart, a late
            # discovery) or the last is a few seconds old, read now, before betting.
            self.take_window_read(ticker, info, terms, now, readings, ctx, weights, late=True)
            plan = self.plan_for(ticker, readings, weights)
            if not plan:
                continue
            snap, p, prob, conf = plan
            setup_names = [x.get("name") for x in ctx.get("chart_setups", {}).get("setups", [])]
            regime = AdaptiveLearning.regime({
                "realized_volatility": ctx.get("spot", {}).get("vol_per_min"),
                "source_divergence_bps": ctx.get("spot", {}).get("source_divergence_bps", 0),
                "book_imbalance": ctx.get("spot", {}).get("book_imbalance_0.1%", 0),
                "trend_strength": min(1.0, max(0.0, float(ctx.get("indicators", {}).get("adx_1m", 0) or 0) / 100.0)),
            })
            adaptive = historical_setup_penalty(self.store, setup_names, regime=regime, report=self.adaptive_report_cache["report"], coin=COINS_TO_COIN.get(info["series"]))
            required_after_learning = float(self.s.crypto_min_edge) + adaptive["surcharge"]
            p["adaptive_learning"] = adaptive
            p["adaptive_required_edge"] = round(required_after_learning, 5)
            if float(p.get("edge", 0) or 0) < required_after_learning:
                self._skip("adaptive setup history requires stronger edge")
                continue
            group_key = (info["series"], close_ts)
            candidate = (float(p.get("edge", 0.0)), float(conf or 0.0),
                         ticker, readings, ctx, plan, lock_key, close_ts)
            if group_key not in candidates or candidate[:2] > candidates[group_key][:2]:
                candidates[group_key] = candidate

        # Markets can expose several strikes/contracts for the same coin/window.
        # Rank them by the live chart/model edge and review only the strongest one.
        for (series, close_ts), candidate in candidates.items():
            edge, conf, ticker, readings, ctx, plan, lock_key, _ = candidate
            reservation = {"state": "pending", "series": series, "close_ts": close_ts,
                           "ticker": ticker, "created_ts": int(now), "mode": self.s.mode}
            # Atomic in SQLite: two separate runner processes cannot both claim
            # the same coin/window between a read and a later write.
            if not self.store.reserve_kv(lock_key, reservation):
                continue
            self.pending.add(ticker)
            try:
                snap, p, prob, conf = plan
                council.convene(self.store, self.s, snap, p, readings, prob, conf, now=now)
            except Exception:
                self.pending.discard(ticker)
                self.store.set_kv(lock_key, None)
                raise
            yield ticker, readings, ctx, plan

    def plan_for(self, ticker, readings, weights, maker=None):
        if not any(r.name == "crypto_model" for r in readings):
            return None
        blend_in, proven = hindsight.split_playbook(readings)
        prob, conf = brain.blend(blend_in, weights)
        info = self.markets[ticker]
        if prob is not None:
            prob = brain.calibrate(prob, self.store, domain=brain.domain_of(info["series"]))
            prob = brain.shrink_to_market(prob, self.books[ticker].mid(), self.store, series=info["series"])
            if proven:      # setups that paid after fees on settled windows: their proven edge is added, not diluted
                prob = clamp(prob + proven, 0.01, 0.99)
            self.last_prob[ticker] = {"prob": round(prob, 4), "conf": round(conf or 0, 3), "ts": round(time.time())}
        snap = {"ticker": ticker, "event_ticker": info["event"], "series": info["series"],
                "team": "UP"}
        if maker is None:
            maker = self.s.order_style == "maker"
            rate, n = self.store.maker_fill_rate()
            # LEARNED: if resting orders keep expiring unfilled, the "better price"
            # is costing us the trade. Below a 25% fill rate, take the ask instead.
            if maker and n >= 20 and rate < float(os.environ.get("MIN_MAKER_FILL_RATE", 0.25)):
                maker = False
        min_edge = self.s.crypto_min_edge
        meta = ml.load_meta()
        if meta and any(r.name == "ml_model" for r in readings):
            # the promoted model says which edge level actually paid on unseen data
            min_edge = max(min_edge, float(meta.get("min_edge", min_edge)))
        if proven:
            # the proven edge is a lower bound net of fees, so a thin margin is expected
            min_edge = min(min_edge, max(0.015, abs(proven) * 0.5))
        # Tighten the required edge when independent model views disagree or
        # reference prices are sparse/divergent. The underlying probability is
        # unchanged; this is a transparent uncertainty margin, not fake alpha.
        analytics = self.coins[info["series"]]
        source_summary = analytics.source_consensus(now=time.time(), max_age=5)
        quality = edge_quality(readings, source_summary)
        required_edge = min_edge + quality["min_edge_surcharge"]
        quality["base_min_edge"] = round(min_edge, 5)
        quality["required_min_edge"] = round(required_edge, 5)
        plan, why = risk.decide(self.s, self.store, snap, prob, conf,
                                self.books[ticker].as_rest_book(),
                                min_edge=required_edge, max_per_bet=self.s.crypto_max_per_bet,
                                maker=maker, raw_model_prob=risk.crypto_model_prob(readings))
        if plan:
            enrich_plan(plan, quality, prob)
        return (snap, plan, prob, conf) if plan else None

    def finish(self, ticker, readings, ctx, plan, verdict):
        """After the council: recheck with the freshest book, then trade."""
        initial_plan = plan[1] if isinstance(plan, (tuple, list)) and len(plan) > 1 else {}
        prior_adaptive = initial_plan.get("adaptive_learning", {}) if isinstance(initial_plan, dict) else {}
        try:
            if verdict["decision"] != "approve":
                self._skip(f"council {verdict['decision']}")
                return
            # Resting maker orders reserve real buying power but are not yet in the
            # ledger. Until reservations are represented in the shared risk planner,
            # allow only one outstanding maker/order-review path in this runner.
            # Otherwise a second approved candidate could spend the same cash.
            if self.makers and ticker not in self.makers:
                self._skip("recheck: pending maker reserves risk budget")
                return
            allowed, reason = guard.status(self.store, self.s.bankroll, mode=self.s.mode)
            if not allowed:
                self.betting_ok, self.betting_why = False, reason
                self._skip(f"execution guard: {reason}")
                return
            fresh, fresh_ctx = self.readings_for(ticker, time.time())
            book_age = self.books[ticker].age()
            if not fresh or book_age is None or book_age > 5:
                self._skip("recheck: data stale")
                return
            again = self.plan_for(ticker, fresh, brain.get_weights(self.store, "crypto"))
            if not again:
                self._skip("recheck: edge gone")
                return
            snap, p, prob, conf = again
            if isinstance(p, dict):
                # Preserve the actual chart/tool snapshot used at the final recheck.
                p["entry_context"] = {
                    key: fresh_ctx[key] for key in (
                        "chart_setups", "multi_timeframe", "indicators", "spot",
                        "price_discovery", "orderbook", "flow"
                    ) if isinstance(fresh_ctx, dict) and key in fresh_ctx
                }
                p["adaptive_learning"] = prior_adaptive if isinstance(prior_adaptive, dict) else {}
            members = council.convene(self.store, self.s, snap, p, fresh, prob, conf)
            if members.get("binding") and members.get("decision") == "pass":
                self._skip("council veto")
                return
            p["council"] = verdict.get("reason", "")
            if p.get("style") == "maker":
                self.start_maker(ticker, snap, p, fresh, prob, conf)
                return
            self.engine.place(snap, p, fresh, prob, conf, summary=self.summary, now=int(time.time()))
            self.summary["alerts"].append(f"{ticker} {p['side'].upper()} @ ${p['price']:.2f} "
                                          f"edge {p['edge']:+.1%} ({verdict['by']})")
        finally:
            self.pending.discard(ticker)
            info = getattr(self, "markets", {}).get(ticker, {})
            terms = info.get("terms", {})
            close_ts = int(terms.get("close_ts") or 0)
            if info.get("series") and close_ts:
                lock_key = self._window_lock_key(info["series"], close_ts)
                reservation = self.store.get_kv(lock_key) or {}
                created_ts = int(reservation.get("created_ts") or 0)
                filled_rows = self.store.q(
                    "SELECT id, order_id FROM bets WHERE ticker=? AND mode=? AND ts>=? LIMIT 1",
                    (ticker, self.s.mode, created_ts),
                )
                if filled_rows:
                    self.store.set_kv(lock_key, {
                        **reservation, "state": "traded",
                        "order_id": filled_rows[0].get("order_id"),
                    })
                elif ticker in self.makers:
                    self.store.set_kv(lock_key, {**reservation, "state": "maker_pending"})
                elif self.store.get_kv("execution_reconciliation_required"):
                    # The exchange may have accepted an order despite a lost response.
                    # Keep this coin/window blocked until reconciled.
                    self.store.set_kv(lock_key, {**reservation, "state": "uncertain"})
                else:
                    # No order was recorded and submission was not ambiguous: permit
                    # a later chart-qualified candidate in this same window.
                    self.store.set_kv(lock_key, None)

    # --- maker orders: rest at a better price, cancel if not filled ---------------------
    def start_maker(self, ticker, snap, plan, readings, prob, conf):
        allowed, reason = guard.status(self.store, self.s.bankroll, mode=self.s.mode)
        if not allowed:
            self.betting_ok, self.betting_why = False, reason
            self._skip(f"maker blocked at execution boundary: {reason}")
            return
        order_id = f"paper-maker-{int(time.time())}"
        real_order = self.s.mode in ("demo", "live")
        # Recovery persistence intentionally tracks one external maker intent.
        # Never allow a second live/demo maker while one is active or uncertain.
        if real_order and self.makers and ticker not in self.makers:
            self._skip("maker blocked: another resting order is active; reconcile it first")
            return
        if real_order:
            # Persist intent BEFORE network I/O. If the process dies or the
            # response is lost after the exchange accepts it, the next run
            # fails closed instead of silently forgetting the possible order.
            self.store.set_kv("maker_recovery", {
                "state": "submitting", "ticker": ticker, "side": plan["side"],
                "contracts": int(plan["contracts"]), "limit": float(plan["limit"]),
                "created_ts": int(time.time()), "mode": self.s.mode,
            })
            try:
                o = self.engine.trade.place_resting_buy(ticker, plan["side"], plan["contracts"],
                                                        plan["limit"], self.s.maker_wait_s + 15)
                order_id = str(o.get("order_id") or "").strip()
                if not order_id:
                    log.error("maker order response has no order ID for %s; cannot safely track it", ticker)
                    self._skip("maker order ID missing; manual reconciliation required")
                    self.betting_ok = False
                    self.betting_why = "maker order ID missing; reconciliation required"
                    self.store.set_kv("execution_reconciliation_required", self.betting_why)
                    return
            except Exception as e:
                log.error("maker order failed %s: %s", ticker, e)
                self.betting_ok = False
                self.betting_why = "maker submission outcome unknown; reconcile exchange orders before resuming"
                self.store.set_kv("execution_reconciliation_required", self.betting_why)
                return
            self.store.set_kv("maker_recovery", {
                "state": "active", "ticker": ticker, "side": plan["side"],
                "contracts": int(plan["contracts"]), "limit": float(plan["limit"]),
                "order_id": order_id, "created_ts": int(time.time()), "mode": self.s.mode,
            })
        self.makers[ticker] = {"snap": snap, "plan": plan, "readings": readings, "prob": prob,
                               "conf": conf, "order_id": order_id, "placed": time.time()}
        log.info("MAKER %s %s x%d resting @ $%.2f", ticker, plan["side"], plan["contracts"],
                 plan["limit"])

    @staticmethod
    def _maker_order_state(order, requested):
        status = str(order.get("status") or "").lower()
        raw_filled = order.get("fill_count_fp")
        if raw_filled in (None, ""):
            raw_filled = order.get("fill_count")
        if raw_filled in (None, ""):
            # Only an explicit executed/filled status proves full execution.
            # Cancellation without cumulative fills is ambiguous, not zero.
            if status in {"executed", "filled"}:
                filled = int(requested)
            else:
                return 0, status or "unknown_fill_count", False
        else:
            try:
                numeric = float(raw_filled)
                if (not math.isfinite(numeric) or numeric < 0
                        or not numeric.is_integer() or numeric > int(requested)):
                    raise ValueError("invalid cumulative fill count")
                filled = int(numeric)
            except (TypeError, ValueError, OverflowError):
                return 0, status or "unknown_fill_count", False
        terminal = status in {"canceled", "cancelled", "executed", "filled", "expired", "rejected"} \
            or filled >= requested
        return filled, status, terminal

    def _cancel_and_reconcile_maker(self, order_id, requested):
        """Cancel any remainder and verify final cumulative fills before ledgering."""
        try:
            self.engine.trade.cancel(order_id)
        except Exception as exc:
            log.warning("maker cancel %s: %s", order_id, exc)
        try:
            order = self.engine.trade.order(order_id)
        except Exception as exc:
            log.warning("maker reconcile %s: %s", order_id, exc)
            return None
        filled, status, terminal = self._maker_order_state(order, requested)
        if not terminal:
            log.warning("maker order %s still %s after cancel; keep tracking", order_id, status or "unknown")
            return None
        return filled

    @staticmethod
    def paper_maker_fill(book, plan):
        """(contracts filled, terminal) for a resting paper buy at plan['limit']."""
        asks = risk.asks_from_book(book.as_rest_book())[plan["side"]]
        through = sum(int(size) for price, size in asks if price < plan["limit"])
        filled = min(int(plan["contracts"]), through)
        return filled, filled > 0

    def check_makers(self, now):
        if self.makers:
            self.betting_ok, self.betting_why = guard.status(
                self.store, self.s.bankroll, mode=self.s.mode
            )
        for ticker, mk in list(self.makers.items()):
            plan, book = mk["plan"], self.books.get(ticker)
            real_order = self.s.mode in ("demo", "live")
            filled, status, terminal = 0, "", False
            if real_order:
                try:
                    order = self.engine.trade.order(mk["order_id"])
                    filled, status, terminal = self._maker_order_state(order, plan["contracts"])
                except Exception as exc:
                    log.warning("maker status %s: %s", ticker, exc)
                    self._skip("maker status unavailable")
                    continue
            elif book is not None:
                # Paper fills require price penetration, not merely a touch, and
                # only up to the size actually offered through our price: a
                # 3-contract ask below the limit can't fill a 40-contract order.
                filled, terminal = self.paper_maker_fill(book, plan)

            edge_gone = self._maker_edge_gone(ticker, plan, now)
            expired = now - mk["placed"] >= self.s.maker_wait_s
            if real_order and not terminal and (
                (filled > 0 and filled < plan["contracts"])
                or (not filled and (edge_gone or expired or not self.betting_ok))
            ):
                # A partial fill does not mean the remainder stopped working.
                # Cancel and verify the final cumulative fill before recording it.
                reconciled = self._cancel_and_reconcile_maker(mk["order_id"], plan["contracts"])
                if reconciled is None:
                    message = f"maker {ticker} cancellation/final fills are unconfirmed; reconcile exchange orders and positions"
                    self._skip("maker cancel unconfirmed; order remains tracked")
                    self.betting_ok, self.betting_why = False, message
                    self.store.set_kv("execution_reconciliation_required", message)
                    continue
                filled, terminal = reconciled, True

            if filled:
                fill_edge = self._side_edge(ticker, plan["side"], plan["limit"], now)
                if fill_edge is not None:
                    plan["fill_edge"] = round(fill_edge, 4)
                self.engine.place(mk["snap"], dict(plan, contracts=filled), mk["readings"],
                                  mk["prob"], mk["conf"], summary=self.summary, now=int(now),
                                  executed={"order_id": mk["order_id"], "contracts": filled})
                self.summary["alerts"].append("{} maker fill {} x{} @ ${:.2f}".format(
                    ticker, plan["side"].upper(), filled, plan["limit"]))
                self.store.log_maker(ticker, 1, "filled", now - mk["placed"])
                if real_order:
                    self.store.set_kv("maker_recovery", None)
                info = getattr(self, "markets", {}).get(ticker, {})
                close_ts = int((info.get("terms") or {}).get("close_ts") or 0)
                if info.get("series") and close_ts:
                    lock_key = self._window_lock_key(info["series"], close_ts)
                    reservation = self.store.get_kv(lock_key) or {}
                    self.store.set_kv(lock_key, {**reservation, "state": "traded",
                                                  "order_id": mk.get("order_id")})
                del self.makers[ticker]
            elif terminal or edge_gone or expired or not self.betting_ok:
                if not self.betting_ok:
                    self._skip(f"maker cancelled: risk guard {self.betting_why}")
                    outcome = "risk_guard"
                elif edge_gone:
                    self._skip("maker cancelled: edge faded")
                    outcome = "edge_faded"
                else:
                    self._skip("maker not filled")
                    outcome = "expired" if expired else "cancelled"
                self.store.log_maker(ticker, 0, outcome, now - mk["placed"])
                if real_order:
                    self.store.set_kv("maker_recovery", None)
                info = getattr(self, "markets", {}).get(ticker, {})
                close_ts = int((info.get("terms") or {}).get("close_ts") or 0)
                if info.get("series") and close_ts:
                    self.store.set_kv(self._window_lock_key(info["series"], close_ts), None)
                del self.makers[ticker]

    def _side_edge(self, ticker, side, price, now):
        readings, _ = self.readings_for(ticker, now)
        if not readings:
            return None
        prob, _ = brain.blend(readings, brain.get_weights(self.store, "crypto"))
        p_side = prob if side == "yes" else 1 - prob
        return p_side - price - risk.taker_fee(100, price, fees.rate(ticker, self.s, maker=True)) / 100

    def _maker_edge_gone(self, ticker, plan, now):
        e = self._side_edge(ticker, plan["side"], plan["limit"], now)
        return e is not None and e < self.s.crypto_min_edge / 2

    # --- early exits: sell when the bid is worth more than holding ----------------------
    def check_exits(self, now):
        weights = brain.get_weights(self.store, "crypto")
        for b in self.store.open_bets(mode=self.s.mode):
            t = b["ticker"]
            if t not in self.markets or t not in self.books or b["series"] == "ARB":
                continue
            levels = self.books[t].as_rest_book().get(b["side"], [])
            bid = levels[0][0] if levels else None
            if bid is None:
                continue
            # The stop-loss takes priority over the model-based early exit: cash out once the
            # bet has lost about MAX_LOSS_PER_BET of its cost, fees included (risk.stop_loss_hit).
            entry_fee_per = b["fee"] / b["contracts"] if b["contracts"] else 0.0
            exit_fee_one = risk.taker_fee(1, bid, fees.rate(t, self.s))
            stop_triggered = risk.stop_loss_hit(b["price"], entry_fee_per, bid, exit_fee_one,
                                                self.s.max_loss_per_bet, self.s.stop_loss_buffer)
            sell_limit = bid
            if stop_triggered:
                # get out of all of it now, taking as many price levels as it needs
                n, avg, sell_limit = risk.sweep_bids(levels, b["contracts"])
                if n < 1:
                    continue
                bid = avg
            else:
                readings, _ = self.readings_for(t, now)
                if not readings:
                    continue
                prob, _ = brain.blend(readings, weights)
                p_side = prob if b["side"] == "yes" else 1 - prob
                # only sell what the best bid can actually absorb
                n = int(min(b["contracts"], levels[0][1]))
                if n < 1:
                    continue
            exit_fee = risk.taker_fee(n, bid, fees.rate(t, self.s))
            # holding is worth p_side per contract; selling is worth the bid minus fees
            if not stop_triggered and bid - exit_fee / n <= p_side + self.s.exit_margin:
                continue
            if self.s.mode in ("demo", "live"):
                try:
                    submitted = self.engine.trade.place_limit_sell(t, b["side"], n, sell_limit)
                    order_id = submitted.get("order_id") or submitted.get("id")
                    if not order_id:
                        raise RuntimeError("sell submission returned no order ID; fill outcome is ambiguous")
                    order = self.engine.trade.order(str(order_id))
                    filled, status, terminal = self._maker_order_state(order, n)
                    if not terminal:
                        raise RuntimeError(
                            f"sell order {order_id} is not confirmed terminal (status={status or 'unknown'})"
                        )
                    n = filled
                except Exception as e:
                    message = f"exit order outcome uncertain for {t}: {e}; reconcile exchange orders and positions"
                    log.error(message)
                    self._skip("exit outcome uncertain; execution paused")
                    self.betting_ok, self.betting_why = False, message
                    self.store.set_kv("execution_reconciliation_required", message)
                    try:
                        alerts.send("URGENT: exit reconciliation required", message,
                                    priority="urgent", tags=["warning"])
                    except Exception:
                        log.exception("could not send exit reconciliation alert")
                    continue
                if n < 1:
                    continue
                exit_fee = risk.taker_fee(n, bid, fees.rate(t, self.s))
            bet_id = b["id"] if n == b["contracts"] else self.store.split_bet(b["id"], n)
            entry_fee = b["fee"] * n / b["contracts"]
            pnl = n * (bid - b["price"]) - entry_fee - exit_fee
            if not self.store.close_bet(bet_id, bid, pnl):
                message = (
                    f"Exchange confirmed an exit for {t}, but ledger row {bet_id} "
                    "was not open when the close was recorded; reconcile exchange orders "
                    "and positions before resuming."
                )
                log.error(message)
                self._skip("exit ledger reconciliation required")
                self.betting_ok, self.betting_why = False, message
                self.store.set_kv("execution_reconciliation_required", message)
                try:
                    alerts.send("URGENT: exit ledger reconciliation required", message,
                                priority="urgent", tags=["warning"])
                except Exception:
                    log.exception("could not send exit ledger reconciliation alert")
                continue
            msg = f"SOLD {t} {b['side'].upper()} x{n} @ ${bid:.2f} (P&L ${pnl:+.2f})"
            self.summary["alerts"].append(msg)
            alerts.send(f"Stop-loss ({self.s.max_loss_per_bet:.0%} max)" if stop_triggered else "Early exit", msg)
            log.info(msg)

    # --- self-learning volatility --------------------------------------------------------
    def _vol_sample(self, series, now, spot, parts):
        key = int(now) - int(now) % 60
        q = self.vol_samples.setdefault(series, {})
        if key not in q:
            q[key] = {"spot": spot, "parts": {k: parts.get(k) for k in ("ewma", "long", "implied")}}

    def learn_vol(self, now, horizon_min=5):
        for series, q in self.vol_samples.items():
            a = self.coins[series]
            mid = a.composite_price(now) or a.book.mid()
            if not mid:
                continue
            for ts in [t for t in q if now - t >= horizon_min * 60]:
                s = q.pop(ts)
                # |move| / sqrt(minutes) / sqrt(2/pi) is an unbiased per-minute sigma
                realized = abs(math.log(mid / s["spot"])) / math.sqrt(horizon_min) / 0.7979
                self.vol_done.setdefault(series, []).append((s["parts"], realized))
            done = self.vol_done.get(series, [])
            if len(done) >= 30:
                volmod.learn_blend(self.store, COINS_TO_COIN[series], done)
                self.vol_done[series] = []

    def refresh_context(self):
        """Slow lookups (funding, options, calendar, DVOL) run here, in a worker
        thread once a minute, never inside the per-second loop."""
        for series, (kr, cb, cur) in COINS.items():
            coin = COINS_TO_COIN[series]
            try:
                if self.with_context:
                    a = self.coins[series]
                    cbp = a.other_prices.get("coinbase", (0, None))[1]
                    self.ctx_cache[series] = marketdata.context(self.store, coin, coinbase_price=cbp, include_spot_15m=True)
                    # token facts: market cap, volume, day range, stablecoin supply (cached, failure-tolerant)
                    self.ctx_cache[series]["token"] = token_data.coin_snapshot(self.store, coin)
                if cur:
                    iv.cached_dvol(self.store, cur, fetch=self.dvol_fetch)
            except Exception as e:
                log.warning("context %s: %s", series, e)

    def _skip(self, why):
        self.summary["skipped"][why] = self.summary["skipped"].get(why, 0) + 1

    # --- async orchestration ---------------------------------------------------------
    async def _poll_crypto_flow(self, ticker, now):
        try:
            await asyncio.to_thread(self.engine.pull_flow, ticker, now)
        except Exception as exc:
            log.warning("crypto trade-flow poll %s: %s", ticker, exc)

    async def main(self, minutes, sports=True, periodic=None, periodic_every=0):
        stop = asyncio.Event()
        if self.seed:
            await asyncio.to_thread(self.seed_history)
        await asyncio.to_thread(self.discover)
        await asyncio.to_thread(self.refresh_context)
        by_kraken = {kr: self.coins[s] for s, (kr, _, _) in COINS.items()}
        by_cb = {cb: self.coins[s] for s, (_, cb, _) in COINS.items() if cb}
        # More CF Benchmarks constituent venues for the composite (BNB isn't listed on either).
        extra = os.environ.get("EXTRA_SPOT_FEEDS", "yes") == "yes"
        by_bitstamp = {cb.split("-")[0].lower() + "usd": self.coins[s]
                       for s, (_, cb, _) in COINS.items() if cb and extra}
        by_gemini = {cb.replace("-", ""): self.coins[s] for s, (_, cb, _) in COINS.items() if cb and extra}
        gemini_books = feeds.GeminiBooks()
        tasks = [
            asyncio.create_task(feeds.run_feed(
                "kraken", feeds.KRAKEN_WS, lambda: feeds.kraken_subs(list(by_kraken)),
                lambda m: feeds.handle_kraken(m, by_kraken),
                lambda: [setattr(a, "gaps", a.gaps + 1) for a in by_kraken.values()], stop)),
            asyncio.create_task(feeds.run_feed(
                "coinbase", feeds.COINBASE_WS, lambda: feeds.coinbase_subs(list(by_cb)),
                lambda m: feeds.handle_coinbase(m, by_cb), lambda: None, stop)),
        ]
        if by_bitstamp:
            tasks.append(asyncio.create_task(feeds.run_feed(
                "bitstamp", feeds.BITSTAMP_WS, lambda: feeds.bitstamp_subs(list(by_bitstamp)),
                lambda m: feeds.handle_bitstamp(m, by_bitstamp), lambda: None, stop)))
        if by_gemini:
            def gemini_gap():
                gemini_books.books.clear()   # a reconnect resends the full book
            tasks.append(asyncio.create_task(feeds.run_feed(
                "gemini", feeds.GEMINI_WS, lambda: feeds.gemini_subs(list(by_gemini)),
                lambda m: feeds.handle_gemini(m, by_gemini, gemini_books), gemini_gap, stop)))
        data_kid, data_pem = self.s.data_key()
        use_ws = bool(data_kid and data_pem)
        kalshi_task = None

        def start_kalshi():
            signer = KalshiClient(self.s.market_data_url(), data_kid, data_pem)
            signer.path_prefix = "/trade-api/ws/v2"
            return asyncio.create_task(feeds.run_feed(
                "kalshi", feeds.KALSHI_WS["demo" if self.s.mode == "demo" else "production"],
                lambda: feeds.kalshi_subs(list(self.books)),
                lambda m: feeds.handle_kalshi(m, self.books, self.ticker_updates, self.ticker_update_history),
                self._mark_kalshi_books_stale, stop,
                headers_fn=lambda: signer._headers("GET", ""),
                on_connect=lambda: setattr(self, "ws_connected", True)))
        if use_ws:
            kalshi_task = start_kalshi()

        end = time.time() + minutes * 60
        last_discover = last_guard = last_poll = last_dump = time.time()
        last_sports, last_save = 0.0, time.time()
        sports_every = float(os.environ.get("SPORTS_EVERY_MINUTES", 12)) * 60
        save_cmd = os.environ.get("SAVE_CMD")
        save_every = float(os.environ.get("SAVE_EVERY_SECONDS", 1800))
        grade_every = float(os.environ.get("GRADE_EVERY_SECONDS", 0))
        handoff_check = os.environ.get("HANDOFF_CHECK", "no") == "yes"
        handoff_every = float(os.environ.get("HANDOFF_EVERY_SECONDS", 120))
        last_grade = last_handoff = last_periodic = time.time()
        handed_off = save_failed = False
        save_fails = 0
        bg = {}

        def spawn(name, job, *args):
            """Run slow work (REST scans, grading, saving) off the loop, one copy per name. The
            loop awaiting it would stop every tick, stop-loss and read for as long as it took:
            ladders and combos alone held it for four minutes at a time."""
            t = bg.get(name)
            if t and not t.done():
                return

            async def run():
                try:
                    if asyncio.iscoroutinefunction(job):
                        await job(*args)
                    else:
                        await asyncio.to_thread(job, *args)
                except Exception as exc:
                    log.error("%s failed: %s", name, exc)
            bg[name] = asyncio.create_task(run())

        async def discover_job():
            nonlocal kalshi_task
            changed = await asyncio.to_thread(self.discover)
            if use_ws and changed:          # resubscribe to the new windows
                kalshi_task.cancel()
                kalshi_task = start_kalshi()

        def save_job():
            # Treat SAVE_CMD as an argument vector, not shell source.
            # This avoids shell injection and reports failed persistence attempts.
            nonlocal save_failed, save_fails
            try:
                subprocess.run(shlex.split(save_cmd), check=True, capture_output=True, text=True)
                if save_fails:
                    log.info("mid-session memory save recovered after %d failed attempts", save_fails)
                save_failed, save_fails = False, 0
            except Exception as exc:
                # say WHY: on 2026-10-28 sixteen failed saves in a row logged only "exit status 1"
                why = " | ".join(x.strip()[-300:] for x in (getattr(exc, "stderr", None), getattr(exc, "stdout", None))
                                 if isinstance(x, str) and x.strip()) or str(exc)
                save_fails += 1
                log.error("mid-session memory save failed (%d in a row): %s", save_fails, why[-600:])
                if save_fails in (1, 3):     # at the start of an outage and again if it persists, not every attempt
                    alerts.send("Memory save failing" if save_fails == 1 else "Memory has not been saved for 3 tries",
                                why[-500:], priority="high", tags=["warning"])
                save_failed = True

        def handoff_job():
            nonlocal handed_off, end
            reason = handoff.restart_reason(os.environ.get("GITHUB_SHA"), os.environ.get("GITHUB_REPOSITORY"),
                                            os.environ.get("GITHUB_TOKEN"))
            if reason and not handed_off:
                handed_off = True
                end = min(end, handoff.next_stop(time.time(), float(os.environ.get("BURST_END_PHASE_SECONDS", 240))))
                log.info("handing over at %s: %s", time.strftime("%H:%M:%S", time.gmtime(end)), reason)

        while time.time() < end:
            now = time.time()
            if now - last_discover >= 60 or self.new_window_missing(now, last_discover):
                last_discover = now
                spawn("discover", discover_job)
            if now - last_poll >= 3:
                # Without the WebSocket, poll everything. WITH it, still poll any
                # book that missed an update or has gone quiet for 8s: if the feed
                # can't log in or silently dies, prices must not freeze.
                if not use_ws:
                    await asyncio.to_thread(self.poll_books)
                elif any(b.needs_resync or b.age() is None or b.age() > 8 for b in self.books.values()):
                    await asyncio.to_thread(self.poll_books, False, True)
                last_poll = now
            if now - last_guard >= 60:
                self.betting_ok, self.betting_why = guard.status(self.store, self.s.bankroll, mode=self.s.mode)
                asyncio.create_task(asyncio.to_thread(self.refresh_context))
                last_guard = now
            # Poll only one due market per main-loop pass, oldest first. A
            # burst over every ticker would serialize REST latency into the
            # inference loop and risk provider rate limits as the universe grows.
            due_flow = [t for t in self.markets
                        if now - self.last_flow_poll.get(t, 0) >= 15]
            if due_flow:
                ticker = min(due_flow, key=lambda t: self.last_flow_poll.get(t, 0))
                self.last_flow_poll[ticker] = now
                asyncio.create_task(self._poll_crypto_flow(ticker, int(now)))
            for ticker, readings, ctx, plan in list(self.tick(now)):
                asyncio.create_task(self._review(ticker, readings, ctx, plan))
            if now - getattr(self, "last_feed_health_publish", 0) >= 1:
                self.publish_feed_health(now)
                self.last_feed_health_publish = now
            if now - last_dump >= 10:
                self.dump(now)
                last_dump = now
            if sports and now - last_sports >= sports_every:
                # the full sports scan + grading + learning, every 12 minutes
                last_sports = now
                spawn("sports", self.sports_cycle)
            if grade_every and now - last_grade >= grade_every:
                # settle and learn while streaming: a long run must not wait for its end to do this
                last_grade = now
                spawn("grade", grade, self.store, self.rest)
            if periodic and periodic_every > 0 and now - last_periodic >= periodic_every:
                last_periodic = now
                spawn("periodic", periodic)
            if handoff_check and now - last_handoff >= handoff_every and not handed_off:
                last_handoff = now
                spawn("handoff", handoff_job)
            if save_cmd and now - last_save >= save_every:
                last_save = now                  # the next attempt is a full interval away, pass or fail
                spawn("save", save_job)
            await asyncio.sleep(max(0.0, 1.0 - (time.time() - now)))
        stop.set()
        pending = [t for t in bg.values() if not t.done()]
        if pending:      # let a save or grading that is mid-flight finish before the post-stream work starts
            await asyncio.wait(pending, timeout=float(os.environ.get("STREAM_DRAIN_SECONDS", 90)))
        for t in tasks + ([kalshi_task] if kalshi_task else []):
            t.cancel()
        for ticker, mk in list(self.makers.items()):
            # Reconcile cumulative fills after cancellation before the process exits;
            # otherwise a last-second fill could be missing from the ledger.
            if self.s.mode in ("demo", "live"):
                filled = None
                for attempt in range(3):
                    filled = await asyncio.to_thread(
                        self._cancel_and_reconcile_maker, mk["order_id"], mk["plan"]["contracts"]
                    )
                    if filled is not None:
                        break
                    if attempt < 2:
                        await asyncio.sleep(1)
                if filled is None:
                    message = (f"Could not confirm cancellation/final fills for {ticker} "
                               f"order {mk['order_id']}; check Kalshi immediately.")
                    log.error(message)
                    self.store.set_kv("execution_reconciliation_required", message)
                    alerts.send("URGENT: maker order status unknown", message,
                                priority="urgent", tags=["warning"])
                    continue
                if filled:
                    self.engine.place(
                        mk["snap"], dict(mk["plan"], contracts=filled), mk["readings"],
                        mk["prob"], mk["conf"], summary=self.summary, now=int(time.time()),
                        executed={"order_id": mk["order_id"], "contracts": filled}
                    )
                    self.store.log_maker(ticker, 1, "filled_on_shutdown", time.time() - mk["placed"])
                    self.store.set_kv("maker_recovery", None)
                else:
                    self.store.log_maker(ticker, 0, "shutdown_cancelled", time.time() - mk["placed"])
                    self.store.set_kv("maker_recovery", None)
            else:
                self.store.log_maker(ticker, 0, "shutdown_cancelled", time.time() - mk["placed"])
        self.makers.clear()
        await asyncio.to_thread(grade, self.store, self.rest)
        return self.summary

    def sports_cycle(self):
        crypto = self.s.crypto_enabled
        self.s.crypto_enabled = False        # live mode already handles crypto
        try:
            out = self.engine.run()
            log.info("sports cycle: %s", out)
        except Exception as e:
            log.error("sports cycle failed: %s", e)
        finally:
            self.s.crypto_enabled = crypto
        try:
            grade(self.store, self.rest)
            if self.llm and briefing.due(self.store):
                briefing.write(self.store, self.llm)
        except Exception as e:
            log.error("grade/briefing failed: %s", e)

    async def _review(self, ticker, readings, ctx, plan):
        verdict = await asyncio.to_thread(council.review, self.llm, self.store, ticker, ctx)
        await asyncio.to_thread(self.finish, ticker, readings, ctx, plan, verdict)

    def dump(self, now, path="data/live_summary.json"):
        """Latest numbers for a dashboard/chart to read."""
        out = {"ts": now, "betting": self.betting_why, "summary": self.summary,
               "coins": {s: a.summary(now) for s, a in self.coins.items()},
               "markets": {t: {"secs_left": round(m["terms"]["close_ts"] - now) if m["terms"]["close_ts"] else None,
                               "strike": m["terms"]["strike"], "kalshi_mid": self.books[t].mid(),
                               "model": self.last_prob.get(t),
                               "ticker": self.ticker_updates.get(t)}
                           for t, m in self.markets.items() if t in self.books},
               "kalshi_feed": {"channel": "ticker + orderbook_delta",
                               "subscribed_markets": len(self.books),
                               "ticker_updates": len(self.ticker_updates),
                               "latest_received_ts": max((v.get("received_ts", 0) for v in self.ticker_updates.values()), default=0)},
               "feed_health": self.publish_feed_health(now)}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(out, f, default=str)
        except OSError:
            pass


def run(settings, store, minutes, sports=True, periodic=None, periodic_every=0):
    return asyncio.run(LiveRunner(settings, store).main(minutes, sports=sports, periodic=periodic,
                                                       periodic_every=periodic_every))
