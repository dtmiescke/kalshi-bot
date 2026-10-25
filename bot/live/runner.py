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

from .. import alerts, brain, briefing, council, crypto_brain, fees, guard, ml, risk
from .. import marketdata, vol as volmod
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
from .book import OrderBook
from .kalshi_book import KalshiBook

log = logging.getLogger("bot.live")

COINS_TO_COIN = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL"}
COINS = {  # Kalshi series -> (Kraken symbol, Coinbase product, Deribit currency)
    "KXBTC15M": ("BTC/USD", "BTC-USD", "BTC"),
    "KXETH15M": ("ETH/USD", "ETH-USD", "ETH"),
    "KXSOL15M": ("SOL/USD", "SOL-USD", None),
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
        self.pending = set()      # tickers under council review
        self.last_saved = {}
        self.last_flow_poll = {}  # REST trade-flow polling is throttled per market
        self.betting_ok, self.betting_why = True, "ok"
        self.summary = {"mode": self.s.mode, "markets": 0, "bets": 0, "skipped": {}, "alerts": []}
        self.seed = seed
        self.with_context = with_context
        self.seed_candles = {}    # series -> Coinbase 1-minute candles (OHLCV)
        self.makers = {}          # ticker -> resting maker order
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
            candles = crypto_sig.fetch_candles(cb)
            self.seed_candles[series] = candles
            for c in candles:
                self.coins[series].mids.append((c["ts"], c["close"]))

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

    def poll_books(self, only_resync=False, only_quiet=False):
        """Without a Kalshi key there's no Kalshi WebSocket: poll books instead.
        With the WebSocket, only books that missed an update are re-fetched."""
        for t, book in self.books.items():
            if only_resync and not book.needs_resync:
                continue
            if only_quiet and not (book.needs_resync or (book.age() or 99) > 8):
                continue
            try:
                ob = self.rest.orderbook(t)
                book.yes = dict(ob.get("yes", []))
                book.no = dict(ob.get("no", []))
                book.last_update = time.time()
                book.needs_resync = False
            except Exception:
                book.gaps += 1

    # --- the per-second brain ------------------------------------------------------
    def readings_for(self, ticker, now):
        """Research one contract with the shared crypto brain on live candles."""
        info, book = self.markets[ticker], self.books[ticker]
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
            context=self.ctx_cache.get(info["series"], {}) if self.with_context else {})
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
        context = {"ticker": ticker, "secs_left": round(terms["close_ts"] - now), "contract": terms,
                   "kalshi_yes_bid": best_yes_bid, "kalshi_yes_ask": best_yes_ask,
                   "kalshi_book_age_s": round(book.age() or 99, 2),
                   "spot": a.summary(now), "market_context": rinfo.get("context", {}),
                   "vol_forecast_per_min": rinfo.get("sigma"),
                   "indicators": {k: round(v, 3) for k, v in feats.items()
                                  if k in ("rsi_1m", "rsi_5m", "rsi_15m", "macd_hist", "bb_pctb",
                                           "adx_1m", "vwap_dist", "ret_5m", "ret_15m",
                                           "vol_ratio_15_60", "dist_sigma")},
                   "signals": {r.name: round(r.prob, 3) for r in readings}}
        return readings, context

    def kalshi_flow_reading(self, ticker, kalshi_mid, now=None):
        """Build an independent signal from anonymous Kalshi prints in the last 5m."""
        now = time.time() if now is None else now
        trades = self.store.flow(ticker, int(now - 300))
        return flow_sig.trade_flow(trades, kalshi_mid)

    def tick(self, now=None):
        now = now or time.time()
        for a in self.coins.values():
            a.sample(now)
        self.check_makers(now)
        if self.s.exits_enabled:
            self.check_exits(now)
        self.learn_vol(now)
        weights = brain.get_weights(self.store, "crypto")
        for ticker, info in list(self.markets.items()):
            if ticker in self.pending or ticker in self.makers or ticker not in self.books:
                continue
            readings, ctx = self.readings_for(ticker, now)
            if not readings:
                continue
            terms = info["terms"]
            self.store.set_kv(f"start_{ticker}", int(terms["close_ts"]))
            self.store.set_kv(f"settle_after_{ticker}", int(terms["close_ts"]) + 300)
            if now - self.last_saved.get(ticker, 0) >= 30:   # learning data, not every tick
                for r in readings:
                    self.store.add_signal(int(now), ticker, r.name, r.prob, r.confidence, r.detail)
                self.last_saved[ticker] = now
            mins_left = (terms["close_ts"] - now) / 60
            if not (self.s.crypto_min_minutes <= mins_left <= self.s.crypto_max_minutes):
                continue
            if not self.betting_ok or guard.league_benched(self.store, info["series"], mode=self.s.mode):
                continue
            plan = self.plan_for(ticker, readings, weights)
            if plan:
                self.pending.add(ticker)
                yield ticker, readings, ctx, plan

    def plan_for(self, ticker, readings, weights, maker=None):
        if not any(r.name == "crypto_model" for r in readings):
            return None
        prob, conf = brain.blend(readings, weights)
        info = self.markets[ticker]
        if prob is not None:
            prob = brain.calibrate(prob, self.store, domain=brain.domain_of(info["series"]))
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
        plan, why = risk.decide(self.s, self.store, snap, prob, conf,
                                self.books[ticker].as_rest_book(),
                                min_edge=min_edge, max_per_bet=self.s.crypto_max_per_bet,
                                maker=maker, raw_model_prob=risk.crypto_model_prob(readings))
        return (snap, plan, prob, conf) if plan else None

    def finish(self, ticker, readings, ctx, plan, verdict):
        """After the council: recheck with the freshest book, then trade."""
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
            fresh, _ = self.readings_for(ticker, time.time())
            if not fresh or (self.books[ticker].age() or 99) > 5:
                self._skip("recheck: data stale")
                return
            again = self.plan_for(ticker, fresh, brain.get_weights(self.store, "crypto"))
            if not again:
                self._skip("recheck: edge gone")
                return
            snap, p, prob, conf = again
            p["council"] = verdict.get("reason", "")
            if p.get("style") == "maker":
                self.start_maker(ticker, snap, p, fresh, prob, conf)
                return
            self.engine.place(snap, p, fresh, prob, conf, summary=self.summary, now=int(time.time()))
            self.summary["alerts"].append(f"{ticker} {p['side'].upper()} @ ${p['price']:.2f} "
                                          f"edge {p['edge']:+.1%} ({verdict['by']})")
        finally:
            self.pending.discard(ticker)

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
                # Paper fills require price penetration, not merely a touch.
                asks = risk.asks_from_book(book.as_rest_book())[plan["side"]]
                if asks and asks[0][0] < plan["limit"]:
                    filled, terminal = plan["contracts"], True

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
            readings, _ = self.readings_for(t, now)
            if not readings:
                continue
            prob, _ = brain.blend(readings, weights)
            p_side = prob if b["side"] == "yes" else 1 - prob
            levels = self.books[t].as_rest_book().get(b["side"], [])
            bid = levels[0][0] if levels else None
            if bid is None:
                continue
            # only sell what the best bid can actually absorb
            n = int(min(b["contracts"], levels[0][1]))
            if n < 1:
                continue
            exit_fee = risk.taker_fee(n, bid, fees.rate(t, self.s))
            # holding is worth p_side per contract; selling is worth the bid minus fees
            if bid - exit_fee / n <= p_side + self.s.exit_margin:
                continue
            if self.s.mode in ("demo", "live"):
                try:
                    submitted = self.engine.trade.place_limit_sell(t, b["side"], n, bid)
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
            alerts.send("Early exit", msg)
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
                    self.ctx_cache[series] = marketdata.context(self.store, coin, coinbase_price=cbp)
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

    async def main(self, minutes, sports=True):
        stop = asyncio.Event()
        if self.seed:
            await asyncio.to_thread(self.seed_history)
        await asyncio.to_thread(self.discover)
        await asyncio.to_thread(self.refresh_context)
        by_kraken = {kr: self.coins[s] for s, (kr, _, _) in COINS.items()}
        by_cb = {cb: self.coins[s] for s, (_, cb, _) in COINS.items()}
        tasks = [
            asyncio.create_task(feeds.run_feed(
                "kraken", feeds.KRAKEN_WS, lambda: feeds.kraken_subs(list(by_kraken)),
                lambda m: feeds.handle_kraken(m, by_kraken),
                lambda: [setattr(a, "gaps", a.gaps + 1) for a in by_kraken.values()], stop)),
            asyncio.create_task(feeds.run_feed(
                "coinbase", feeds.COINBASE_WS, lambda: feeds.coinbase_subs(list(by_cb)),
                lambda m: feeds.handle_coinbase(m, by_cb), lambda: None, stop)),
        ]
        data_kid, data_pem = self.s.data_key()
        use_ws = bool(data_kid and data_pem)
        kalshi_task = None

        def start_kalshi():
            signer = KalshiClient(self.s.market_data_url(), data_kid, data_pem)
            signer.path_prefix = "/trade-api/ws/v2"
            return asyncio.create_task(feeds.run_feed(
                "kalshi", feeds.KALSHI_WS["production"],
                lambda: feeds.kalshi_subs(list(self.books)),
                lambda m: feeds.handle_kalshi(m, self.books), lambda: None, stop,
                headers_fn=lambda: signer._headers("GET", "")))
        if use_ws:
            kalshi_task = start_kalshi()

        end = time.time() + minutes * 60
        last_discover = last_guard = last_poll = last_dump = time.time()
        last_sports, last_save = 0.0, time.time()
        sports_every = float(os.environ.get("SPORTS_EVERY_MINUTES", 12)) * 60
        save_cmd = os.environ.get("SAVE_CMD")
        while time.time() < end:
            now = time.time()
            if now - last_discover >= 60:
                changed = await asyncio.to_thread(self.discover)
                last_discover = now
                if use_ws and changed:          # resubscribe to the new windows
                    kalshi_task.cancel()
                    kalshi_task = start_kalshi()
            if now - last_poll >= 3:
                # Without the WebSocket, poll everything. WITH it, still poll any
                # book that missed an update or has gone quiet for 8s: if the feed
                # can't log in or silently dies, prices must not freeze.
                if not use_ws:
                    await asyncio.to_thread(self.poll_books)
                elif any(b.needs_resync or (b.age() or 99) > 8 for b in self.books.values()):
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
            if now - last_dump >= 10:
                self.dump(now)
                last_dump = now
            if sports and now - last_sports >= sports_every:
                # the full sports scan + grading + learning, every 12 minutes
                await asyncio.to_thread(self.sports_cycle)
                last_sports = now
            if save_cmd and now - last_save >= 1800:
                # Treat SAVE_CMD as an argument vector, not shell source.
                # This avoids shell injection and reports failed persistence attempts.
                try:
                    await asyncio.to_thread(
                        subprocess.run, shlex.split(save_cmd), check=True,
                        capture_output=True, text=True
                    )
                    last_save = now
                except Exception as exc:
                    log.error("mid-session memory save failed: %s", exc)
                    alerts.send("Memory save failed", str(exc)[:500],
                                priority="high", tags=["warning"])
            await asyncio.sleep(max(0.0, 1.0 - (time.time() - now)))
        stop.set()
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
                               "strike": m["terms"]["strike"], "kalshi_mid": self.books[t].mid()}
                           for t, m in self.markets.items() if t in self.books}}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(out, f, default=str)
        except OSError:
            pass


def run(settings, store, minutes, sports=True):
    return asyncio.run(LiveRunner(settings, store).main(minutes, sports=sports))
