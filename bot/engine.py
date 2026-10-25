"""One research-and-trade cycle. Safe to run every 5-15 minutes."""
import json
import math
import os
import logging
import threading
import time
import uuid
from datetime import datetime

from . import alerts, brain, council, fees, guard, risk
from .llm import LLM
from .config import CRYPTO_15M, SPORTS
from .kalshi_client import KalshiClient, num, price
from .matching import match_event
from .odds import OddsClient, consensus, next_game_ts as odds_next_game, record_spend, refresh_plan
from . import crypto_brain
from .live import settlement
from .signals import crypto as crypto_sig
from .signals import implied_vol as iv
from .signals import flow as flow_sig
from .signals import intel
from .signals import market_signals as ms
from .signals import schedule as sched
from .signals.base import Reading, clamp
from .signals.crypto_microstructure import summarize_kalshi_book

log = logging.getLogger("bot")


# Serialize final risk recheck, order placement, and ledger update across async reviewers.
_EXECUTION_LOCK = threading.RLock()


def _rotating_event_batch(events, cursor="", budget=150, held=()):
    """Prioritize imminent games while preserving rotation and held-event coverage."""
    from .event_clock import event_start_ts
    unique = {}
    for event in events:
        ticker = str(event.get("event_ticker") or event.get("ticker") or "")
        if ticker:
            unique.setdefault(ticker, event)
    ordered = sorted(unique.items())
    held_set = {str(x) for x in held if x}
    held_rows = [(ticker, event) for ticker, event in ordered if ticker in held_set]
    eligible = [(ticker, event) for ticker, event in ordered if ticker not in held_set]
    limit = max(0, int(budget))
    if not eligible or limit == 0:
        return [event for _, event in held_rows], str(cursor or "")
    now = time.time()
    priority = []
    for ticker, event in eligible:
        start = event_start_ts(event)
        if start is None:
            continue
        delta = start - now
        if -3 * 3600 <= delta <= 90 * 60:
            priority.append((0 if delta >= 0 else 1, abs(delta), ticker, event))
    priority.sort(key=lambda item: (item[0], item[1], item[2]))
    urgent_count = min(len(priority), max(1, limit // 3)) if priority else 0
    urgent = [(ticker, event) for _, _, ticker, event in priority[:urgent_count]]
    urgent_tickers = {ticker for ticker, _ in urgent}
    rotating = [(ticker, event) for ticker, event in eligible if ticker not in urgent_tickers]
    cursor = str(cursor or "")
    after = [(ticker, event) for ticker, event in rotating if ticker > cursor]
    before = [(ticker, event) for ticker, event in rotating if ticker <= cursor]
    rotating_selected = (after + before)[:max(0, limit - len(urgent))]
    selected = urgent + rotating_selected
    next_cursor = rotating_selected[-1][0] if rotating_selected else cursor
    return [event for _, event in held_rows + selected], next_cursor


def _series_sport(series, metadata=None):
    """Map a discovered Kalshi series to a known odds key or a research category."""
    if series in SPORTS:
        return SPORTS[series]
    meta = metadata or {}
    text = " ".join([str(series), str(meta.get("title", "")), str(meta.get("category", "")),
                     " ".join(str(v) for v in (meta.get("tags") or []))]).lower()
    # Apply exclusions before broad category keywords so excluded sports cannot
    # fall through as "other" and evade the intended exclusion.
    rules = (
        (("mma", "ufc", "mixed martial arts", "ultimate fighting"), "mma"),
        (("boxing", "boxer"), "boxing"),
        (("tennis", "atp", "wta"), "tennis"),
        (("motorsport", "formula 1", "formula-one", "f1", "nascar", "indycar", "racing"), "motorsports"),
        (("golf", "pga", "lpga", "liv golf"), "golf"),
        (("esports", "e-sports", "league of legends", "counter-strike", "dota", "valorant"), "esports"),
        # Keep pro NFL and college football as distinct research domains. Exact
        # configured series still take precedence via SPORTS above this fallback.
        (("nfl", "national football league"), "americanfootball_nfl"),
        (("college football", "ncaa football", "ncaaf", "cfb"), "americanfootball_ncaaf"),
        (("football",), "football"),
        (("basketball", "wnba", "nba", "ncaab"), "basketball"),
        (("baseball", "mlb"), "baseball"),
        (("hockey", "nhl"), "hockey"),
        (("soccer", "premier league", "champions league", "mls"), "soccer"),
        (("cricket",), "cricket"),
        (("rugby",), "rugby"),
        (("volleyball",), "volleyball"),
        (("chess",), "chess"),
        (("badminton",), "badminton"),
        (("cycling", "tour de france"), "cycling"),
        (("athletics", "track and field", "track_field"), "track_field"),
        (("swimming",), "swimming"),
        (("olympics", "olympic"), "olympics"),
    )
    for words, category in rules:
        if any(word in text for word in words):
            return category
    return "other"


# Every discovered Kalshi sport is eligible for research. Categories without
# external odds coverage still need sufficient independent evidence to qualify.
EXCLUDED_SPORT_CATEGORIES = set()


def _sports_excluded(series, metadata=None):
    """Compatibility helper: no sport category is blanket-excluded."""
    return _series_sport(series, metadata) in EXCLUDED_SPORT_CATEGORIES


def _fresh_odds_history(rows, now, max_age_seconds):
    """Drop stale/future sportsbook rows before they can manufacture apparent edge."""
    cutoff = int(now) - max(0, int(max_age_seconds))
    return [row for row in rows
            if cutoff <= int(row.get("ts", 0)) <= int(now)]


def iso_to_ts(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (AttributeError, ValueError):
        return None


class Engine:
    def __init__(self, settings, store, data_client=None, trade_client=None, odds_client=None,
                 price_feed=None, llm=None, schedule_fetch=None, spot_feed=None):
        self.s = settings
        self.store = store
        # real prices to beat always come from production market data
        self.data = data_client or KalshiClient(settings.market_data_url(), *settings.data_key())
        self.trade = trade_client
        if self.trade is None and settings.mode in ("demo", "live"):
            self.trade = KalshiClient(settings.base_url(), settings.kalshi_key_id,
                                      settings.kalshi_private_key)
        self.odds = odds_client or OddsClient(settings.odds_api_key)
        self.odds_max_age = settings.odds_max_age_minutes
        self.price_feed = price_feed or crypto_sig.fetch_candles
        self.spot_feed = spot_feed or crypto_sig.fetch_spot
        self.llm = llm if llm is not None else (
            LLM(settings.openrouter_key, store) if settings.ai_enabled() else None)
        self.schedule_fetch = schedule_fetch or sched.fetch_games
        self.public = ms.load_public_betting()
        self.betting_ok = True
        self._headlines = {}
        fees.load(store)             # last refreshed Kalshi fee table

    @staticmethod
    def _confirmed_ioc_fill(order, requested):
        """Fail closed unless an IOC response identifies the order and fill count."""
        if not isinstance(order, dict):
            raise RuntimeError("exchange returned no usable order object")
        order_id = order.get("order_id") or order.get("id")
        if not order_id:
            raise RuntimeError("exchange response missing order ID; fill outcome uncertain")
        raw = order.get("fill_count_fp")
        if raw in (None, ""):
            raw = order.get("fill_count")
        if raw in (None, ""):
            raise RuntimeError("exchange response missing cumulative fill count")
        try:
            value = float(raw)
            if not math.isfinite(value) or value < 0 or not value.is_integer():
                raise ValueError("fill count is not a finite non-negative integer")
            filled = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(f"invalid cumulative fill count: {raw!r}") from exc
        if filled > int(requested):
            raise RuntimeError(f"exchange fill count {filled} exceeds requested {requested}")
        return str(order_id), filled

    def _pause_for_reconciliation(self, reason):
        """Persist a hard stop when an order outcome may be unknown to the client."""
        message = f"order outcome requires exchange reconciliation: {reason}"
        try:
            self.store.set_kv("execution_reconciliation_required", message)
        except Exception:
            log.exception("CRITICAL: failed to persist execution reconciliation pause")
        self.betting_ok = False
        log.error(message)
        try:
            alerts.send("URGENT: order reconciliation required", message,
                        priority="urgent", tags=["warning"])
        except Exception:
            log.exception("could not send order reconciliation alert")

    # --- data collection -------------------------------------------------------
    def refresh_odds(self, sport, next_game=None):
        """Pull books when the free credit budget allows (odds.refresh_plan:
        only with a game within 36 h, hourly near game time, every
        ODDS_REFRESH_MINUTES otherwise, the month's credits paced by day), and
        store vig-free consensus. Stale lines never trade: evaluate() refuses
        consensus older than 1.5 x ODDS_MAX_AGE_MINUTES."""
        now = int(time.time())
        due, why = refresh_plan(self.store, sport, now, next_game, self.s.odds_refresh_minutes,
                                self.s.odds_regions, int(os.environ.get("ODDS_MONTHLY_CREDITS", 500)))
        if not due:
            log.debug("odds %s not refreshed: %s", sport, why)
            return
        events = self.odds.fetch(sport, self.s.odds_regions)
        record_spend(self.store, self.odds, now, self.s.odds_regions,
                     int(os.environ.get("ODDS_MONTHLY_CREDITS", 500)), alert=alerts.send)
        slim = []
        for ev in events:
            for team, (p, n, spread) in consensus(ev).items():
                self.store.add_odds({
                    "ts": now, "sport": sport, "event_id": ev["id"],
                    "commence_time": ev.get("commence_time"), "home": ev.get("home_team"),
                    "away": ev.get("away_team"), "team": team, "fair_prob": p,
                    "n_books": n, "raw": {"spread": spread}})
            slim.append({k: ev.get(k) for k in ("id", "home_team", "away_team", "commence_time")})
        # Empty/error-free responses must clear the cached event list too; keeping
        # yesterday's events after a failed refresh can create stale matches.
        self.store.set_kv(f"odds_events_{sport}", slim)
        self.store.set_kv(f"odds_last_{sport}", time.time())
        log.info("odds %s: %d events, credits left %s", sport, len(events), self.odds.last_remaining)

    def snapshot(self, ev, m, now):
        row = {
            "ts": now, "ticker": m["ticker"], "event_ticker": ev["event_ticker"],
            "series": ev.get("series_ticker"), "title": m.get("title") or ev.get("title"),
            "team": m.get("yes_sub_title") or m.get("subtitle"),
            "yes_bid": price(m, "yes_bid"), "yes_ask": price(m, "yes_ask"),
            "last_price": price(m, "last_price"), "volume": num(m, "volume"),
            "open_interest": num(m, "open_interest"),
            "close_time": m.get("close_time"), "status": m.get("status"),
        }
        # Keep the original, bounded contract terms in the in-memory candidate
        # context for sports counsel. Store.add_market_snapshot persists only its
        # established schema, so this does not alter the database or dashboard.
        for key in ("rules_primary", "rules_secondary", "early_close_condition",
                    "settlement_value", "settlement_source", "custom_strike"):
            value = m.get(key)
            if value not in (None, ""):
                row[key] = str(value)[:3000]
        self.store.add_market_snapshot(row)
        return row

    def pull_flow(self, ticker, now):
        since = self.store.get_kv(f"flow_last_{ticker}", now - 6 * 3600)
        try:
            raw = self.data.trades(ticker, min_ts=since)
        except Exception as e:
            log.warning("trades %s: %s", ticker, e)
            return
        trades = []
        for t in raw:
            ts = iso_to_ts(t.get("created_time")) or now
            trades.append({"trade_id": t.get("trade_id") or str(uuid.uuid4()),
                           "ticker": ticker, "ts": ts, "count": num(t, "count"),
                           "yes_price": price(t, "yes_price"),
                           "taker_side": t.get("taker_side", "")})
        self.store.add_flow_trades(trades)
        self.store.set_kv(f"flow_last_{ticker}", now)

    # --- research ----------------------------------------------------------------
    def research(self, mkt, ev_match, book_team, opp_team, sport, now):
        readings = []
        r = ms.kalshi_market(mkt)
        if not r:
            return []
        readings.append(r)
        mid = r.detail["mid"]

        hist = self.store.odds_history(ev_match["id"], book_team) if ev_match else []
        hist = _fresh_odds_history(hist, now, self.odds_max_age * 60)
        if hist:
            last = hist[-1]
            bc = ms.book_consensus(last["fair_prob"], last["n_books"],
                                   last["raw"].get("spread", 0.0))
            bc.detail["age_min"] = round((now - last["ts"]) / 60, 1)
            readings.append(bc)
            lm = ms.line_movement(hist, now)
            if lm:
                readings.append(lm)
            rlm = ms.reverse_line_movement(book_team, hist, self.public.get(book_team))
            if rlm:
                readings.append(rlm)
            games = sched.cached_games(self.store, sport, fetch=self.schedule_fetch)
            sp = sched.schedule_spot(book_team, opp_team, book_team == ev_match.get("home_team"),
                                     ev_match.get("commence_time", ""), games,
                                     last["fair_prob"], sport)
            if sp:
                readings.append(sp)

        window = now - 3 * 3600
        trades = self.store.flow(mkt["ticker"], window)
        recent_hist = [h for h in hist if h["ts"] >= window]
        book_move = (recent_hist[-1]["fair_prob"] - recent_hist[0]["fair_prob"]
                     if len(recent_hist) >= 2 else None)
        tf = flow_sig.trade_flow(trades, mid, book_move)
        if tf:
            readings.append(tf)

        context = {r.name: {k: v for k, v in r.detail.items()
                            if k in ("open", "now", "steam_logit", "team", "opponent",
                                     "big_imbalance", "kalshi_leads_books")}
                   for r in readings if r.name != "kalshi_market"}
        news = self.news_reading(mkt, ev_match, book_team, opp_team, sport, mid, hist, now,
                                 context)
        if news:
            readings.append(news)

        # proven track record on this team/series scales confidence
        mult, _ = intel.track_record(self.store, f"team:{book_team or mkt['team']}")
        for x in readings:
            if x.name != "kalshi_market":
                x.confidence = min(1.0, x.confidence * mult)
        for x in readings:
            self.store.add_signal(now, mkt["ticker"], x.name, x.prob, x.confidence, x.detail)
        return readings

    def news_reading(self, mkt, ev_match, team, opp, sport, mid, hist, now, context=None):
        key = f"news_reading_{mkt['ticker']}"
        start = iso_to_ts(ev_match.get("commence_time")) if ev_match else None
        if not (self.llm and team and start and 0 < start - now < 24 * 3600):
            return None

        category_for = {
            "americanfootball_nfl": "football", "americanfootball_ncaaf": "football",
            "basketball_nba": "basketball", "basketball_wnba": "basketball",
            "basketball_ncaab": "basketball", "baseball_mlb": "baseball",
            "icehockey_nhl": "hockey", "soccer_mls": "soccer",
            "soccer_epl": "soccer", "soccer_uefa": "soccer",
            "tennis_atp": "tennis", "tennis_wta": "tennis",
            "mma_mixed_martial_arts": "mma", "golf_pga": "golf",
            "motorsport_f1": "motorsports", "esports": "esports",
        }
        try:
            from . import sports_intel
            category = category_for.get(sport, sport if sport in sports_intel.SPORT_TERMS else None)
        except Exception:
            sports_intel = None
            category = category_for.get(sport)

        if intel.news_due(self.store, mkt["ticker"],
                          minutes=max(5, int(os.environ.get("SPORTS_AI_NEWS_REFRESH_MINUTES", "30")))):
            if sport not in self._headlines:
                self._headlines[sport] = intel.fetch_headlines(sport)
                if sports_intel:
                    try:
                        now_ts = int(time.time())
                        categories = [category] if category else []
                        categories.append("event_news")
                        for news_category in set(categories):
                            for item in sports_intel.recent(self.store, limit=250, category=news_category, kind="news"):
                                published = item.get("published_ts")
                                if published and now_ts - published > 48 * 3600:
                                    continue
                                item_event = item.get("event_ticker")
                                self._headlines[sport].append({
                                    "headline": f"[{item.get('publisher') or item.get('source') or 'unknown source'}] {item.get('title', '')}",
                                    "description": item.get("summary", ""),
                                    "published": published or item.get("observed_ts"),
                                    "source": item.get("source", ""),
                                    "url": item.get("url", ""),
                                    "event_ticker": item_event,
                                })
                    except Exception as e:
                        log.debug("cached web intelligence unavailable for %s: %s", sport, e)

            event_headlines = [h for h in self._headlines[sport]
                               if not h.get("event_ticker") or h.get("event_ticker") == mkt.get("event_ticker")]
            heads = intel.relevant(event_headlines, [team, opp or ""])
            ai_context = dict(context or {})
            if sports_intel:
                stat_categories = {
                    "americanfootball_nfl": ["nfl"], "americanfootball_ncaaf": ["college_football"],
                    "basketball_nba": ["nba"], "basketball_wnba": ["wnba"],
                    "baseball_mlb": ["mlb"], "icehockey_nhl": ["nhl"],
                    "soccer_epl": ["soccer_epl"], "soccer_mls": ["soccer_mls"],
                    "soccer_uefa": ["soccer_uefa"], "tennis_atp": ["tennis_atp"],
                    "tennis_wta": ["tennis_wta"], "golf_pga": ["golf_pga"],
                    "mma_mixed_martial_arts": ["mma_ufc"],
                }
                try:
                    stat_rows = []
                    team_terms = [str(x).lower() for x in (team, opp) if x and len(str(x)) > 2]
                    for stat_category in stat_categories.get(sport, []):
                        for kind in ("stats", "score"):
                            for item in sports_intel.recent(self.store, limit=100, category=stat_category, kind=kind):
                                # Do not let a stale box score or an unrelated event
                                # masquerade as current matchup evidence. Public feeds
                                # can retain old games and entity names can overlap.
                                observed = int(item.get("observed_ts") or 0)
                                age_seconds = max(0, int(time.time()) - observed) if observed else None
                                if age_seconds is None or age_seconds > 72 * 3600:
                                    continue
                                serialized = json.dumps(item, ensure_ascii=False).lower()
                                if team_terms and not all(term in serialized for term in team_terms):
                                    continue
                                stat_rows.append({
                                    "event": item.get("title") or item.get("name"), "observed_ts": observed,
                                    "evidence_age_seconds": age_seconds,
                                    "source": item.get("source"), "verified": item.get("verified", False),
                                    "state": item.get("state"), "scoreboard": item.get("scoreboard") or item.get("competitors", []),
                                    "team_stats": item.get("team_stats", []), "player_stats": item.get("player_stats", []),
                                    "leaders": item.get("leaders", []), "injuries": item.get("injuries"),
                                    "lineups": item.get("lineups"), "rosters": item.get("rosters"),
                                    "depth_charts": item.get("depth_charts"),
                                    "season_series": item.get("season_series"),
                                    "game_info": item.get("game_info"),
                                    "football_matchup_profile": item.get("football_matchup_profile"),
                                    "event_evidence_profile": item.get("event_evidence_profile"),
                                    "public_odds": item.get("odds", []), "predictor": item.get("predictor"),
                                })
                    if stat_rows:
                        ai_context["recent_scoreboard_stats"] = stat_rows[:3]
                except Exception as e:
                    log.debug("cached scoreboard stats unavailable for %s: %s", sport, e)

            # OpenRouter web search augments the existing evidence path; it does not
            # create a second collector or bypass the current signal/risk engine.
            try:
                from . import research_brain
                web_evidence = research_brain.event_research(
                    self.llm, self.store,
                    event_ticker=mkt.get("event_ticker") or mkt.get("ticker"),
                    sport=sport, team=team, opponent=opp,
                    event_start_ts=start, market_price=mid,
                    book_prob=hist[-1]["fair_prob"] if hist else None,
                    context=ai_context,
                )
                if web_evidence:
                    heads = (heads + [web_evidence])[-15:]
                    ai_context["open_web_research"] = {
                        "summary": web_evidence.get("description", "")[:3000],
                        "sources": web_evidence.get("research_sources", [])[:8],
                        "source": web_evidence.get("source"),
                    }
            except Exception as exc:
                log.debug("OpenRouter event research unavailable for %s: %s",
                          mkt.get("event_ticker") or mkt.get("ticker"), exc)

            book = hist[-1]["fair_prob"] if hist else None
            r = intel.ai_news(self.llm, team, opp, mid, book, heads, ai_context)
            self.store.set_kv(f"news_last_{mkt['ticker']}", time.time())
            self.store.set_kv(key, r.__dict__ if r else None)
        cached = self.store.get_kv(key)
        return intel.Reading(**cached) if cached else None

    # --- the cycle ---------------------------------------------------------------
    def run(self, watch=None, sports=True, event_budget=None):
        """watch: optional Watchlist. When given, only watched games (plus games
        the bot already holds) are researched — a fast spot check."""
        self.s.validate()
        now = int(time.time())
        summary = {"mode": "batch" if event_budget is not None else ("watch" if watch else "full"),
                   "markets": 0, "matched": 0, "bets": 0, "skipped": {}, "alerts": []}
        if event_budget is not None:
            summary["event_budget"] = int(event_budget)
        held = {b["event_ticker"] for b in self.store.open_bets(mode=self.s.mode)}
        if sports and event_budget is None and getattr(self.s, "combo_research_enabled", True):
            from . import combo_research
            summary["combos"] = combo_research.scan(self.data, self.store, settings=self.s)
        weights = brain.get_weights(self.store, "sports")
        self.betting_ok, why = guard.status(self.store, self.s.bankroll, mode=self.s.mode)
        summary["betting"] = why
        if not self.betting_ok:
            log.warning("betting paused (%s): researching and learning only", why)
            if self.store.get_kv("pause_alerted") != why:
                alerts.send("Betting paused", why, priority="high", tags=["warning"])
                self.store.set_kv("pause_alerted", why)
        else:
            self.store.set_kv("pause_alerted", None)

        series_map = {} if event_budget is not None else dict(SPORTS)
        events_by_series = {}
        selected = self.s.selected_series() if hasattr(self.s, "selected_series") else None
        if sports and getattr(self.s, "sports_mode", "configured") in ("all", "configured"):
            try:
                catalog = self.data.sports_series()
                if self.s.sports_mode == "configured":
                    # Default mode stays within the currently configured leagues;
                    # switch SPORTS_MODE=all explicitly to discover every Kalshi sport.
                    catalog = {key: value for key, value in catalog.items() if key in SPORTS}
                all_events = self.data.sports_events_all()
                total_events = len(all_events)
                cursor = str(self.store.get_kv("sports_event_cursor", "") or "")
                # Keep the full eligible event set for the read-only fleet. The
                # rotating budget is applied only after the fleet has scanned it.
                for ev in all_events:
                    series = ev.get("series_ticker")
                    if not series:
                        event_ticker = str(ev.get("event_ticker") or "")
                        series = next((known for known in catalog if event_ticker.startswith(known + "-")), None)
                    if not series or series not in catalog:
                        continue
                    if selected is not None and series not in selected:
                        continue
                    ev.setdefault("series_ticker", series)
                    series_map[series] = _series_sport(series, catalog.get(series))
                    events_by_series.setdefault(series, []).append(ev)
                self.store.set_kv("sports_catalog_health", {
                    "ts": now, "series_discovered": len(catalog),
                    "open_events_seen": total_events,
                    "events_scanned": len(all_events), "event_budget": event_budget,
                    "event_cursor": cursor,
                    "sports_events_matched": sum(map(len, events_by_series.values())),
                    "coverage_warning": total_events >= 4000 or len(catalog) >= 10000,
                    "mode": (("dynamic_all" if self.s.sports_mode == "all" else "configured")
                             + ("_rotating" if event_budget is not None else ""))})
            except Exception as e:
                log.warning("dynamic sports discovery failed; using configured series: %s", e)
                events_by_series = {}
                series_map = dict(SPORTS)
        elif sports and getattr(self.s, "sports_mode", "configured") == "manual":
            selected = selected or set()
            series_map = {series: SPORTS.get(series, _series_sport(series))
                          for series in selected}
            self.store.set_kv("sports_catalog_health", {
                "ts": now, "series_selected": sorted(selected), "mode": "manual"})
        else:
            series_map = dict(SPORTS) if getattr(self.s, "sports_mode", "configured") == "configured" else {}

        if sports and not events_by_series and series_map:
            fallback_events = []
            for selected_series in series_map:
                try:
                    found = self.data.sports_events(selected_series)
                    for ev in found:
                        ev.setdefault("series_ticker", selected_series)
                    fallback_events.extend(found)
                except Exception as e:
                    log.warning("events %s: %s", selected_series, e)
            for ev in fallback_events:
                selected_series = ev.get("series_ticker")
                if selected_series:
                    events_by_series.setdefault(selected_series, []).append(ev)
            for selected_series in series_map:
                events_by_series.setdefault(selected_series, [])
        if sports and events_by_series:
            # Eight read-only specialist workers inspect every eligible event before
            # the deep-analysis budget is applied. Only the existing Engine can trade.
            try:
                from .sports_fleet import scan_fleet
                events_by_series, scanner_health, live_watch = scan_fleet(
                    events_by_series, now=now, workers=8)
                self.store.set_kv("sports_scanner_health", scanner_health)
                catalog_health = self.store.get_kv("sports_catalog_health", {}) or {}
                self.store.set_kv("sports_coverage_audit", {
                    "ts": now, "scanner_ok": bool(scanner_health.get("ok")),
                    "events_unique": scanner_health.get("events_unique", 0),
                    "events_with_start_time": scanner_health.get("events_with_start_time", 0),
                    "events_missing_start_time": scanner_health.get("events_missing_start_time", 0),
                    "events_without_markets": scanner_health.get("events_without_markets", 0),
                    "active_markets": scanner_health.get("active_markets", 0),
                    "active_markets_with_quotes": scanner_health.get("active_markets_with_quotes", 0),
                    "active_markets_missing_quotes": scanner_health.get("active_markets_missing_quotes", 0),
                    "series_coverage": scanner_health.get("series_coverage", {}),
                    "catalog_coverage_warning": bool(catalog_health.get("coverage_warning")),
                    "catalog_events_seen": catalog_health.get("open_events_seen"),
                    "catalog_series_discovered": catalog_health.get("series_discovered"),
                    "coverage_status": ("catalog_truncated_or_at_limit" if catalog_health.get("coverage_warning")
                                        else "observed_catalog; upstream completeness not guaranteed"),
                    "execution_enabled": False,
                })
                self.store.set_kv("sports_live_watch", live_watch[:500])
            except Exception as e:
                log.exception("sports scanner fleet failed: %s", e)
                summary["scanner_fleet_error"] = type(e).__name__
            if event_budget is not None:
                cursor = str(self.store.get_kv("sports_event_cursor", "") or "")
                all_unique = [ev for series in sorted(events_by_series)
                              for ev in events_by_series.get(series, [])]
                selected_events, cursor = _rotating_event_batch(
                    all_unique, cursor, event_budget, held=held)
                selected_by_series = {}
                for ev in selected_events:
                    series = ev.get("series_ticker")
                    if series:
                        selected_by_series.setdefault(series, []).append(ev)
                for known_series in series_map:
                    selected_by_series.setdefault(known_series, [])
                events_by_series = selected_by_series
                self.store.set_kv("sports_event_cursor", cursor)
                self.store.set_kv("sports_deep_scan_budget", {
                    "ts": now, "events_available": len(all_unique),
                    "events_selected": len(selected_events), "event_budget": event_budget,
                    "event_cursor": cursor})
            for known_series in series_map:
                events_by_series.setdefault(known_series, [])
        if sports and events_by_series and os.environ.get("SPORTS_INTEL_EVENT_SEARCH", "no").lower() in ("yes", "true", "1"):
            try:
                from . import sports_intel
                candidates = [ev for grouped in events_by_series.values() for ev in grouped]
                # Upcoming event news is the most actionable. Unknown start times sort last.
                def event_start(ev):
                    value = iso_to_ts(ev.get("start_time") or ev.get("commence_time") or ev.get("date"))
                    return value if value and value >= now else now + 10 * 86400
                candidates.sort(key=event_start)
                event_news = sports_intel.collect_event_news(
                    self.store, candidates, max_events=int(os.environ.get("SPORTS_INTEL_EVENT_BUDGET", 12)), now=now)
                self.store.set_kv("sports_intel_event_news_health", {"ts": now, **event_news})
            except Exception as e:
                log.warning("event-specific news collection failed: %s", e)

        market_budget = None
        if event_budget is not None:
            market_budget = max(25, min(1000, int(os.environ.get("SPORTS_SCAN_MARKET_BUDGET", "100"))))
        sports_markets_processed = 0
        sports_markets_deferred = 0
        for series, sport in (series_map.items() if sports else ()):
            if watch and not watch.wants_series(series) and not any(
                    h.startswith(series) for h in held):
                continue
            try:
                if series in events_by_series:
                    events = events_by_series[series]
                else:
                    # Fallback for the original configured leagues when dynamic
                    # catalog endpoints are unavailable; manual selections are explicit.
                    events = self.data.sports_events(series)
            except Exception as e:
                log.warning("events %s: %s", series, e)
                continue
            if not events:
                continue
            supported_odds = sport in set(SPORTS.values())
            if supported_odds:
                try:
                    self.refresh_odds(sport, odds_next_game(events, now))
                except Exception as e:
                    log.warning("odds %s: %s", sport, e)
                odds_events = self.store.get_kv(f"odds_events_{sport}", [])
            else:
                odds_events = []

            for ev in events:
                ev.setdefault("series_ticker", series)
                if watch and ev["event_ticker"] not in held and not watch.matches(ev):
                    continue
                markets = [m for m in ev.get("markets", [])
                           if m.get("status") in ("active", "open")]
                # Bound expensive per-market history/research work. The fast
                # scanner fleet already inspected all eligible events. Always
                # revisit events with open positions, even after the budget.
                event_ticker = str(ev.get("event_ticker") or "")
                if (market_budget is not None and event_ticker not in held
                        and sports_markets_processed >= market_budget):
                    sports_markets_deferred += len(markets)
                    continue
                sports_markets_processed += len(markets)
                teams = [m.get("yes_sub_title") for m in markets if m.get("yes_sub_title")]
                ktime = next((m.get("expected_expiration_time") or m.get("close_time")
                              for m in markets), None)
                mapping, ev_match = match_event(teams, ktime, odds_events)
                if ev_match:
                    summary["matched"] += 1
                    start = iso_to_ts(ev_match.get("commence_time"))
                    for m in markets:
                        self.store.set_kv(f"start_{m['ticker']}", start)

                if self.betting_ok and ev_match and len(markets) == 2:
                    if self.try_arb(ev, markets, summary, now):
                        continue
                benched = guard.league_benched(self.store, series, mode=self.s.mode)
                candidates = []   # pick the single best bet per game
                for m in markets:
                    summary["markets"] += 1
                    snap = self.snapshot(ev, m, now)
                    self.pull_flow(m["ticker"], now)
                    team = snap["team"]
                    book_team = mapping.get(team) if mapping else None
                    opp = next((v for k, v in (mapping or {}).items() if k != team), None)

                    readings = self.research(snap, ev_match, book_team, opp, sport, now)
                    if not readings:
                        continue
                    start = iso_to_ts(ev_match["commence_time"]) if ev_match else None
                    if not start or start <= now:
                        # in-game prices move faster than our book data: research
                        # and log, but only bet pre-game for now
                        self._skip(summary, "not pre-game or unmatched")
                        continue
                    if not self.betting_ok or benched:
                        self._skip(summary, "league benched" if benched else "betting paused")
                        continue
                    # Apply league-specific learned weights when the series is identifiable.
                    league_weights = brain.get_weights(self.store, brain.learning_domain_of(series))
                    c = self.evaluate(snap, readings, league_weights, summary)
                    if c:
                        candidates.append(c)
                if candidates:
                    best = max(candidates, key=lambda c: c[1]["edge"])
                    # Contract rules often are omitted from nested event payloads.
                    # Fetch the full market only for the shortlisted candidate (not every
                    # market), and merge rule text only; never replace the fresh price snapshot.
                    rule_keys = ("rules_primary", "rules_secondary", "early_close_condition",
                                 "settlement_value", "settlement_source", "custom_strike")
                    if not best[0].get("rules_primary") and not best[0].get("settlement_source"):
                        try:
                            details = self.data.market(best[0]["ticker"])
                            for key in rule_keys:
                                value = details.get(key) if isinstance(details, dict) else None
                                if value not in (None, ""):
                                    best[0][key] = str(value)[:3000]
                        except Exception as exc:
                            log.debug("contract-rule lookup unavailable for %s: %s",
                                      best[0].get("ticker"), exc)
                    # Sport-specific counsel is a veto/wait layer only; it cannot
                    # create candidates, alter sizing, or bypass the existing risk gate.
                    from . import sports_counsel
                    counsel = sports_counsel.review(
                        self.llm, self.store, ev, best[0], best[1], best[2],
                        best[3], best[4], now=now)
                    self.store.set_kv("sports_counsel_last_decision", {
                        "ts": now, "ticker": best[0]["ticker"], **counsel})
                    summary.setdefault("sports_counsel", {})
                    choice = counsel.get("decision", "wait")
                    summary["sports_counsel"][choice] = summary["sports_counsel"].get(choice, 0) + 1
                    if choice != "approve":
                        reason = "sports counsel " + choice + ": " + str(counsel.get("reason", "no reason supplied"))
                        self._record_decision(
                            best[0], best[2], "rejected", reason, best[3], best[4],
                            side=best[1].get("side"), market_price=best[1].get("price"),
                            edge=best[1].get("edge"), now=now)
                        self._skip(summary, reason)
                        continue
                    if watch:
                        summary["alerts"].append(
                            f"{best[0]['ticker']} {best[1]['side'].upper()} @ "
                            f"${best[1]['price']:.2f}, edge {best[1]['edge']:+.1%}")
                    self.place(*best, summary=summary, now=now)
        if market_budget is not None:
            summary["sports_deep_scan"] = {
                "market_budget": market_budget, "markets_in_selected_events": sports_markets_processed,
                "markets_deferred": sports_markets_deferred,
                "full_event_scan": True, "held_events_prioritized": True}
            self.store.set_kv("sports_deep_scan_health", {"ts": now, **summary["sports_deep_scan"]})
            coverage = self.store.get_kv("sports_coverage_audit", {}) or {}
            deep_budget = self.store.get_kv("sports_deep_scan_budget", {}) or {}
            coverage.update({
                "deep_scan": dict(summary["sports_deep_scan"]),
                "events_available_for_deep_scan": deep_budget.get("events_available"),
                "events_selected_for_deep_scan": deep_budget.get("events_selected"),
                "event_rotation_cursor": deep_budget.get("event_cursor"),
            })
            self.store.set_kv("sports_coverage_audit", coverage)
        if self.s.crypto_enabled:
            self.run_crypto(watch, brain.get_weights(self.store, "crypto"), summary, now)
        log.info("cycle: %s", summary)
        return summary

    # --- crypto 15-minute markets ------------------------------------------------
    def run_crypto(self, watch, weights, summary, now):
        ladder = []
        for series, pair in CRYPTO_15M.items():
            if watch and series not in watch.series:
                continue
            try:
                events = self.data.sports_events(series)
            except Exception as e:
                log.warning("events %s: %s", series, e)
                continue
            if not events:
                continue
            candles = self.price_feed(pair)
            if not candles:
                self._skip(summary, "no crypto price feed")
                continue
            spot, spot_ts = self.spot_feed(pair)
            spot_fresh = spot is not None and now - spot_ts <= 15
            for ev in events:
                ev.setdefault("series_ticker", series)
                for m in ev.get("markets", []):
                    if m.get("status") not in ("active", "open"):
                        continue
                    summary["markets"] += 1
                    close = iso_to_ts(m.get("close_time"))
                    if not close:
                        continue
                    snap = self.snapshot(ev, m, now)
                    ladder.append(self._ladder_row(m, snap))
                    self.store.set_kv(f"start_{m['ticker']}", close)
                    self.store.set_kv(f"settle_after_{m['ticker']}", close + 300)
                    self.pull_flow(m["ticker"], now)
                    left = (close - now) / 60
                    readings = self.crypto_research(snap, m, candles, left, now,
                                                    spot=spot if spot_fresh else None)
                    if not readings:
                        continue
                    if not spot_fresh:
                        self._skip(summary, "spot price not fresh")
                        continue
                    if not self.betting_ok or guard.league_benched(self.store, series, mode=self.s.mode):
                        self._skip(summary, "betting paused")
                        continue
                    if not (self.s.crypto_min_minutes <= left <= self.s.crypto_max_minutes):
                        self._skip(summary, "crypto window timing")
                        continue
                    c = self.evaluate(snap, readings, weights, summary,
                                      required="crypto_model",
                                      min_edge=self.s.crypto_min_edge,
                                      max_per_bet=self.s.crypto_max_per_bet)
                    if c:
                        self.place(*c, summary=summary, now=now)
        self.scan_ladders(ladder, summary, now)

    @staticmethod
    def _ladder_row(m, snap):
        t = settlement.contract_terms(m)
        return {"ticker": m["ticker"], "strike": t["strike"], "close_ts": t["close_ts"],
                "event_ticker": snap.get("event_ticker"), "series": snap.get("series"),
                "rules_key": f"{t['benchmark']}|{t['avg_seconds']}|{snap['series'][:5]}",
                "yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask")}

    def collect_ladder_markets(self, ladder, now):
        """Hourly/daily 'above $X' crypto ladders (CRYPTO_LADDER_SERIES).
        Verify the tickers in the Kalshi app; unknown ones are skipped."""
        import os
        for series in [x for x in os.environ.get("CRYPTO_LADDER_SERIES", "KXBTCD,KXETHD").split(",") if x]:
            try:
                events = self.data.sports_events(series)
            except Exception:
                continue
            for ev in events[:6]:
                ev.setdefault("series_ticker", series)
                for m in ev.get("markets", []):
                    if m.get("status") in ("active", "open") and \
                            m.get("strike_type") in ("greater", "greater_or_equal", None):
                        snap = {"series": series, "yes_bid": price(m, "yes_bid"),
                                "yes_ask": price(m, "yes_ask")}
                        ladder.append(self._ladder_row(m, snap))

    def run_ladders(self, summary, now=None):
        """Hourly/daily 'BTC above $X at time T' contracts, traded DIRECTIONALLY
        with the same exact-contract model, plus the ladder arbitrage scan.
        Stricter than 15-minute trading because the trained model can't help
        here: bigger edge required, smaller bets, one strike per event."""
        import os
        now = now or int(time.time())
        min_edge = float(os.environ.get("LADDER_MIN_EDGE", 8)) / 100
        max_bet = float(os.environ.get("LADDER_MAX_PER_BET", 5))
        lo_min = float(os.environ.get("LADDER_MIN_MINUTES", 20))
        hi_min = float(os.environ.get("LADDER_MAX_MINUTES", 240))
        weights = brain.get_weights(self.store, "crypto")
        ladder, cache = [], {}
        for series in [x for x in os.environ.get("CRYPTO_LADDER_SERIES", "KXBTCD,KXETHD").split(",") if x]:
            try:
                events = self.data.sports_events(series)
            except Exception:
                continue
            coin = crypto_brain.coin_from_series(series)
            pair = f"{coin}-USD"
            for ev in events[:8]:
                ev.setdefault("series_ticker", series)
                cands = []
                for m in ev.get("markets", []):
                    if m.get("status") not in ("active", "open") or \
                            m.get("strike_type") not in ("greater", "greater_or_equal", None):
                        continue
                    snap = self.snapshot(ev, m, now)
                    ladder.append(self._ladder_row(m, snap))
                    t = settlement.contract_terms(m)
                    if not t["close_ts"]:
                        continue
                    left = (t["close_ts"] - now) / 60
                    self.store.set_kv(f"start_{m['ticker']}", int(t["close_ts"]))
                    self.store.set_kv(f"settle_after_{m['ticker']}", int(t["close_ts"]) + 600)
                    if not (lo_min <= left <= hi_min) or not self.betting_ok or \
                            guard.league_benched(self.store, series, mode=self.s.mode):
                        continue
                    if pair not in cache:
                        cache[pair] = (self.price_feed(pair), *self.spot_feed(pair))
                    candles, spot, spot_ts = cache[pair]
                    if not candles or spot is None or now - spot_ts > 15:
                        continue
                    readings = self.crypto_research(snap, m, candles, left, now, spot=spot)
                    if len(readings) < 2:
                        continue
                    c = self.evaluate(snap, readings, weights, summary, required="crypto_model",
                                      min_edge=min_edge, max_per_bet=max_bet)
                    if c:
                        cands.append(c)
                if cands:   # one strike per event: the best one
                    self.place(*max(cands, key=lambda c: c[1]["edge"]), summary=summary, now=now)
        self.scan_ladders(ladder, summary, now)
        return summary

    def crypto_research(self, snap, m, candles, minutes_left, now, spot=None):
        terms = settlement.contract_terms(m)
        readings, info = crypto_brain.research(
            self.store, snap["ticker"], snap["series"], terms, candles,
            snap.get("yes_bid"), snap.get("yes_ask"), now=now, spot=spot)
        if not readings:
            return []
        # Reuse the existing KalshiClient and its binary-market orderbook
        # semantics. Cache per ticker so repeat reviews don't hammer the API.
        ticker = snap["ticker"]
        book_key = f"kalshi_crypto_book_{ticker}"
        cached = self.store.get_kv(book_key)
        book_info = cached.get("value") if isinstance(cached, dict) and now - cached.get("ts", 0) < 5 else None
        if book_info is None:
            try:
                raw_book = self.data.orderbook(ticker, depth=20)
                book_info = summarize_kalshi_book(raw_book)
                if book_info:
                    self.store.set_kv(book_key, {"ts": now, "value": book_info})
            except Exception:
                book_info = None
        if book_info:
            imbalance = max(-1.0, min(1.0, float(book_info["depth_imbalance"])))
            mid = readings[0].detail.get("mid")
            if isinstance(mid, (int, float)):
                # Depth is contextual evidence, not an independent forecast.
                # Bound the adjustment to 1.5 probability points and keep its
                # confidence low until out-of-sample learning proves value.
                adjusted = clamp(mid + 0.015 * imbalance, 0.02, 0.98)
                readings.append(Reading("crypto_kalshi_orderbook", adjusted, 0.25, {
                    "venue": "kalshi", "depth_imbalance": round(imbalance, 4),
                    "yes_bid_depth": round(book_info["yes_bid_depth"], 4),
                    "no_bid_depth": round(book_info["no_bid_depth"], 4),
                    "yes_levels": book_info["yes_levels"], "no_levels": book_info["no_levels"],
                    "adjustment_cap": 0.015,
                }))
        tf = flow_sig.trade_flow(self.store.flow(snap["ticker"], now - 900),
                                 readings[0].detail["mid"])
        if tf:
            readings.append(tf)
        for x in readings:
            self.store.add_signal(now, snap["ticker"], x.name, x.prob, x.confidence, x.detail)
        return readings

    def scan_ladders(self, crypto_markets, summary, now):
        """Cross-market arbitrage across every crypto 'above strike' contract."""
        found = guard.ladder_arbs(crypto_markets, self.s.taker_fee_rate)
        for a in found[:3]:
            summary.setdefault("arbs", []).append(
                f"ladder {a['low']} YES + {a['high']} NO: +{a['profit_per']:.1%} per $1 locked")
            alerts.send("Arbitrage found", summary["arbs"][-1], priority="high", tags=["moneybag"])
            if not self.betting_ok:
                continue
            self.execute_ladder(a, now, summary)

    def _arbitrage_budget(self, event_ticker):
        """Combined two-leg budget respecting global caps and current account mode."""
        acct = self.store.account(self.s.bankroll, mode=self.s.mode)
        if acct["open_positions"] + 2 > self.s.max_open_positions:
            return 0.0
        left_day = self.s.max_per_day - self.store.staked_since(
            risk.day_start_ts(), mode=self.s.mode
        )
        left_game = self.s.max_per_game - self.store.staked_on_event(
            event_ticker, mode=self.s.mode
        )
        left_open = self.s.max_open_cost - acct["open_cost"] - 0.05
        left_cash = acct["cash"] - 0.05
        total_cap = min(2 * self.s.max_per_bet, left_day, left_game, left_open, left_cash)
        if not math.isfinite(total_cap) or total_cap <= 0:
            return 0.0
        return max(0.0, total_cap / (1 + fees.rate(event_ticker, self.s)))

    def _unwind_leg(self, ticker, side, price, order_id, filled,
                    event_ticker, series, team=None):
        """Unwind a partially built arbitrage and ledger any residual exposure."""
        if filled <= 0:
            return 0
        unwound = 0
        failure = None
        try:
            book = self.data.orderbook(ticker)
            bid = risk.bid_for(book, side)
            if not bid:
                raise RuntimeError("no bid available to unwind")
            close = self.trade.place_limit_sell(ticker, side, filled, bid)
            # A successful HTTP response is not proof of a confirmed sell fill.
            # Require the exchange order ID and a bounded cumulative fill count;
            # otherwise stop and reconcile rather than silently guessing.
            _, unwound = self._confirmed_ioc_fill(close, filled)
            if unwound < filled:
                failure = f"unwind only filled {unwound}/{filled}"
        except Exception as exc:
            failure = str(exc)
            self._pause_for_reconciliation(
                f"sell/unwind submission or confirmation uncertain for {ticker}: {exc}"
            )
        residual = max(0, filled - unwound)
        if residual:
            # Do not leave a partial fill invisible to account exposure/risk checks.
            try:
                self.store.add_bet({
                    "ts": int(time.time()), "mode": self.s.mode, "ticker": ticker,
                    "event_ticker": event_ticker or ticker, "series": series or "ARB",
                    "team": team, "side": side, "price": price, "contracts": residual,
                    "stake": round(residual * price, 2),
                    "fee": risk.taker_fee(residual, price, fees.rate(series or ticker, self.s)),
                    "model_prob": None, "edge": None, "confidence": 1.0,
                    "signals": {"arbitrage": {"unwind_residual": True,
                                              "original_order_id": order_id,
                                              "filled_before_unwind": filled,
                                              "unwound": unwound,
                                              "failure": failure}},
                    "order_id": order_id, "style": "taker",
                })
            except Exception as ledger_error:
                failure = f"{failure or 'unwind incomplete'}; residual ledger failed: {ledger_error}"
            message = (f"{ticker}: {residual} contract(s) remain after arbitrage unwind "
                       f"(original order {order_id}). {failure or ''} Check position manually.")
            self._pause_for_reconciliation(message)
            log.error(message)
            alerts.send("URGENT: arbitrage unwind incomplete", message,
                        priority="urgent", tags=["warning"])
        return residual

    def execute_ladder(self, a, now, summary):
        """Serialize arbitrage execution with every other order path."""
        with _EXECUTION_LOCK:
            return self._execute_ladder_locked(a, now, summary)

    def _execute_ladder_locked(self, a, now, summary):
        """Re-check both books and global risk caps, then buy YES(low) + NO(high)."""
        try:
            b_low, b_high = self.data.orderbook(a["low"]), self.data.orderbook(a["high"])
        except Exception:
            return
        yes_low = risk.asks_from_book(b_low)["yes"]
        no_high = risk.asks_from_book(b_high)["no"]
        if not yes_low or not no_high:
            return
        (pl, ql), (ph, qh) = yes_low[0], no_high[0]
        event_ticker = a.get("event_ticker") or a["low"]
        budget = self._arbitrage_budget(event_ticker)
        per_leg_cap = int(self.s.max_per_bet // max(pl, ph)) if max(pl, ph) > 0 else 0
        n = int(min(ql, qh, per_leg_cap, budget // (pl + ph))) if budget > 0 else 0
        rate = fees.rate(a.get("series") or a["low"], self.s)
        cost_fees = risk.taker_fee(n, pl, rate) + risk.taker_fee(n, ph, rate)
        if n < 1 or n * (1 - pl - ph) - cost_fees <= 0:
            return
        allowed, reason = guard.status(self.store, self.s.bankroll, mode=self.s.mode)
        if not allowed:
            log.warning("ladder arbitrage blocked at execution boundary: %s", reason)
            return
        placed = []   # track each leg's actual fills before checking completeness
        for ticker, side, px in ((a["low"], "yes", pl), (a["high"], "no", ph)):
            oid = f"paper-ladder-{uuid.uuid4().hex[:8]}"
            filled = n
            if self.s.mode in ("demo", "live"):
                try:
                    o = self.trade.place_limit_buy(ticker, side, n, px)
                    oid, filled = self._confirmed_ioc_fill(o, n)
                    placed.append((ticker, side, px, oid, filled))
                    if filled < n:
                        raise RuntimeError(f"leg only filled {filled}/{n} contracts")
                except Exception as e:
                    self._pause_for_reconciliation(
                        f"ladder order outcome uncertain for {ticker}: {e}"
                    )
                    log.error("LADDER LEG FAILED %s: %s", ticker, e)
                    for t, sd, px, original_order_id, qty in placed:
                        self._unwind_leg(
                            t, sd, px, original_order_id, qty, event_ticker,
                            a.get("series") or "ARB"
                        )
                    return
            else:
                placed.append((ticker, side, px, oid, filled))
        for ticker, side, px, oid, filled in placed:
            if filled != n:
                log.error("LADDER incomplete fill unexpectedly reached ledger: %s %s/%s",
                          ticker, filled, n)
                return
            self.store.add_bet({
                "ts": now, "mode": self.s.mode, "ticker": ticker, "event_ticker": event_ticker,
                "series": a.get("series") or "ARB", "team": None, "side": side, "price": px, "contracts": filled,
                "stake": round(filled * px, 2), "fee": risk.taker_fee(filled, px, fees.rate(ticker, self.s)),
                "model_prob": None, "edge": None, "confidence": 1.0,
                "signals": {"arbitrage": {"pair": [a["low"], a["high"]],
                                          "locked": round(filled * (1 - pl - ph) - fees, 2)}},
                "order_id": oid, "style": "taker"})
        summary["bets"] += 2

    def try_arb(self, ev, markets, summary, now):
        """Serialize sports arbitrage with other order paths."""
        with _EXECUTION_LOCK:
            return self._try_arb_locked(ev, markets, summary, now)

    def _try_arb_locked(self, ev, markets, summary, now):
        """Buy YES on both teams when together they cost < $1 after fees."""
        if self.store.has_open_event_bet(ev["event_ticker"], mode=self.s.mode):
            return False
        try:
            books = [self.data.orderbook(m["ticker"]) for m in markets]
        except Exception:
            return False
        budget = self._arbitrage_budget(ev["event_ticker"])
        rate = fees.rate(ev["series_ticker"], self.s)
        plan = guard.arb_plan(ev["series_ticker"], books[0], books[1], rate, budget)
        if not plan:
            return False
        # Each leg is an individual bet and must independently respect MAX_PER_BET.
        per_leg_cap = int(self.s.max_per_bet // max(plan["price_a"], plan["price_b"]))
        if plan["contracts"] > per_leg_cap:
            plan = guard.arb_plan(
                ev["series_ticker"], books[0], books[1], rate,
                per_leg_cap * (plan["price_a"] + plan["price_b"])
            )
        if not plan:
            return False
        allowed, reason = guard.status(self.store, self.s.bankroll, mode=self.s.mode)
        if not allowed:
            log.warning("sports arbitrage blocked at execution boundary: %s", reason)
            return False
        legs = [(markets[0], plan["price_a"]), (markets[1], plan["price_b"])]
        order_ids = []
        for m, px in legs:
            oid = f"paper-arb-{uuid.uuid4().hex[:8]}"
            filled = plan["contracts"]
            if self.s.mode in ("demo", "live"):
                try:
                    o = self.trade.place_limit_buy(m["ticker"], "yes", plan["contracts"], px)
                    oid, filled = self._confirmed_ioc_fill(o, plan["contracts"])
                    order_ids.append((m, px, oid, filled))
                    if filled < plan["contracts"]:
                        raise RuntimeError(f"leg only filled {filled}/{plan['contracts']} contracts")
                except Exception as e:
                    self._pause_for_reconciliation(
                        f"sports arbitrage order outcome uncertain for {m['ticker']}: {e}"
                    )
                    log.error("ARB LEG FAILED %s: %s", m["ticker"], e)
                    self.unwind(order_ids, plan["contracts"],
                                event_ticker=ev["event_ticker"], series=ev["series_ticker"])
                    return False
            else:
                order_ids.append((m, px, oid, filled))
        for m, px, oid, filled in order_ids:
            if filled != plan["contracts"]:
                log.error("ARB incomplete fill unexpectedly reached ledger: %s %s/%s",
                          m["ticker"], filled, plan["contracts"])
                return False
            fee = risk.taker_fee(filled, px, rate)
            self.store.add_bet({
                "ts": now, "mode": self.s.mode, "ticker": m["ticker"],
                "event_ticker": ev["event_ticker"], "series": ev["series_ticker"],
                "team": m.get("yes_sub_title"), "side": "yes", "price": px,
                "contracts": filled, "stake": round(filled * px, 2),
                "fee": fee, "model_prob": None, "edge": None, "confidence": 1.0,
                "signals": {"arbitrage": {"locked_profit": plan["locked_profit"]}},
                "order_id": oid})
        summary["bets"] += len(order_ids)
        summary.setdefault("arbs", []).append(
            f"{ev['event_ticker']}: locked ${plan['locked_profit']:.2f} on ${plan['stake']:.2f}")
        log.info("ARB %s locked $%.2f", ev["event_ticker"], plan["locked_profit"])
        return True

    def unwind(self, legs, contracts, event_ticker=None, series=None):
        """Sell back actual filled quantities and ledger any residual exposure."""
        for m, px, order_id, filled in legs:
            self._unwind_leg(
                m["ticker"], "yes", px, order_id, filled,
                event_ticker or m.get("event_ticker") or m["ticker"],
                series or m.get("series") or "ARB", team=m.get("yes_sub_title")
            )

    def _skip(self, summary, reason):
        # group by the reason's wording, without the numbers
        key = " ".join(w for w in reason.split() if not any(c.isdigit() for c in w))[:40]
        summary["skipped"][key] = summary["skipped"].get(key, 0) + 1

    def _record_decision(self, snap, readings, decision, reason=None, prob=None, conf=None, side=None, market_price=None, edge=None, now=None):
        """Append a decision snapshot; this does not place or simulate an order."""
        if market_price is None:
            bid, ask = snap.get("yes_bid"), snap.get("yes_ask")
            last = snap.get("last_price")
            market_price = ((float(bid) + float(ask)) / 2 if bid is not None and ask is not None
                            else float(last) if last is not None else None)
        if side is None and prob is not None:
            side = "yes" if prob >= 0.5 else "no"
        side_prob = (prob if side == "yes" else 1 - prob) if prob is not None and side in ("yes", "no") else None
        self.store.add_decision_audit({
            "ts": int(now or time.time()), "ticker": snap["ticker"],
            "event_ticker": snap.get("event_ticker"), "series": snap.get("series"),
            "decision": decision, "reason": reason, "side": side,
            "market_price": market_price, "model_prob": prob, "side_prob": side_prob,
            "edge": edge, "confidence": conf,
            "signals": {r.name: {"prob": r.prob, "confidence": r.confidence, "detail": r.detail} for r in readings}})

    def evaluate(self, snap, readings, weights, summary, required="book_consensus",
                 min_edge=None, max_per_bet=None):
        if not any(r.name == required for r in readings):
            self._record_decision(snap, readings, "rejected", "no outside signal")
            self._skip(summary, "no outside signal")
            return None
        bc = next((r for r in readings if r.name == "book_consensus"), None)
        if bc and bc.detail.get("age_min", 0) > self.odds_max_age * 1.5:
            self._record_decision(snap, readings, "rejected", "sportsbook odds too old")
            self._skip(summary, "sportsbook odds too old")
            return None
        prob, conf = brain.blend(readings, weights)
        if prob is not None:
            # Calibrate using settled canonical audits across candidates and
            # rejections, not just the subset the bot happened to bet.
            prob = brain.calibrate(prob, self.store, domain=brain.domain_of(snap.get("series", "")))
        if prob is None:
            self._record_decision(snap, readings, "rejected", "no calibrated probability", conf=conf)
            return None
        try:
            book = self.data.orderbook(snap["ticker"])
        except Exception as e:
            self._record_decision(snap, readings, "rejected", "orderbook unavailable", prob, conf)
            log.warning("orderbook %s: %s", snap["ticker"], e)
            return None
        plan, why = risk.decide(self.s, self.store, snap, prob, conf, book,
                                min_edge=min_edge, max_per_bet=max_per_bet,
                                raw_model_prob=risk.crypto_model_prob(readings))
        if not plan:
            self._record_decision(snap, readings, "rejected", why, prob, conf)
            self._skip(summary, why)
            return None
        # Preserve any strategy-specific limits so the final execution-boundary
        # recheck cannot accidentally loosen a stricter caller's budget.
        plan["execution_min_edge"] = float(min_edge if min_edge is not None else self.s.min_edge)
        plan["execution_max_per_bet"] = float(
            max_per_bet if max_per_bet is not None else self.s.max_per_bet
        )
        # The council only sits on math-approved candidates. Advisory members change
        # nothing; only members who EARNED veto power can turn this into a pass.
        verdict = council.convene(self.store, self.s, snap, plan, readings, prob, conf)
        if verdict.get("binding") and verdict.get("decision") == "pass":
            self._record_decision(snap, readings, "rejected", verdict["reason"], prob, conf,
                                  side=plan["side"], market_price=plan["price"], edge=plan["edge"])
            self._skip(summary, "council veto")
            return None
        self._record_decision(snap, readings, "candidate", "passed deterministic risk checks",
                              prob, conf, side=plan["side"], market_price=plan["price"], edge=plan["edge"])
        return snap, plan, readings, prob, conf
    def place(self, snap, plan, readings, prob, conf, summary, now, executed=None):
        """Serialize execution so concurrent reviewers cannot spend the same risk budget."""
        with _EXECUTION_LOCK:
            return self._place_locked(snap, plan, readings, prob, conf, summary, now, executed)

    def _place_locked(self, snap, plan, readings, prob, conf, summary, now, executed=None):
        """Send (or simulate) the order and record the bet. `executed` = an order
        the live runner already filled as a resting maker order."""
        if not executed:
            # Recheck the hard circuit breakers at the final execution boundary,
            # not only at the start of a long scan. Already-filled orders must
            # still be recorded even if a breaker trips afterward.
            allowed, reason = guard.status(self.store, self.s.bankroll, mode=self.s.mode)
            if not allowed:
                self._skip(summary, f"execution guard: {reason}")
                log.warning("order blocked at execution boundary: %s", reason)
                return
            # Rebuild the plan against the latest book and latest account exposure.
            # The lock makes the risk check + ledger update atomic among async reviewers.
            try:
                fresh_book = self.data.orderbook(snap["ticker"])
            except Exception as exc:
                self._skip(summary, f"execution recheck: orderbook unavailable ({exc})")
                log.warning("execution recheck failed for %s: %s", snap["ticker"], exc)
                return
            is_crypto = str(snap.get("series", "")).endswith("15M")
            fresh_plan, why = risk.decide(
                self.s, self.store, snap, prob, conf, fresh_book,
                min_edge=plan.get(
                    "execution_min_edge",
                    self.s.crypto_min_edge if is_crypto else self.s.min_edge
                ),
                max_per_bet=plan.get(
                    "execution_max_per_bet",
                    self.s.crypto_max_per_bet if is_crypto else self.s.max_per_bet
                ),
                maker=False,
                raw_model_prob=risk.crypto_model_prob(readings),
            )
            if not fresh_plan:
                self._skip(summary, f"execution recheck: {why}")
                log.info("execution recheck blocked %s: %s", snap["ticker"], why)
                return
            plan = fresh_plan
        order_id = f"paper-{uuid.uuid4().hex[:10]}"
        if executed:
            order_id = executed.get("order_id", order_id)
            plan["contracts"] = executed.get("contracts", plan["contracts"])
            # Ledger only the filled quantity. A partial maker fill must not
            # retain the original full-order stake.
            plan["stake"] = round(plan["contracts"] * plan["price"], 2)
            # Resting maker fills use the maker fee schedule.
            plan["fee"] = risk.taker_fee(plan["contracts"], plan["price"],
                                         fees.rate(snap.get("series") or snap["ticker"], self.s, maker=True))
        elif self.s.mode in ("demo", "live"):
            try:
                order = self.trade.place_limit_buy(snap["ticker"], plan["side"],
                                                   plan["contracts"], plan["limit"])
                order_id, filled = self._confirmed_ioc_fill(order, plan["contracts"])
                if filled < 1:
                    self._skip(summary, "order not filled")
                    return
                cost = num(order, "taker_fill_cost") + num(order, "maker_fill_cost")
                if cost > 0:
                    # record what we ACTUALLY paid, not what we planned to pay
                    plan["price"] = round(cost / filled / (100 if cost / filled > 1 else 1), 4)
                plan["contracts"] = filled
                plan["stake"] = round(filled * plan["price"], 2)
                maker_filled = int(num(order, "maker_fill_count") or 0)
                taker_filled = int(num(order, "taker_fill_count") or max(0, filled - maker_filled))
                # Recompute fees on actual fill counts and the observed average
                # price. If the exchange omits fill-type counts, conservatively
                # treat the unclassified fills as taker fills.
                series = snap.get("series") or snap["ticker"]
                plan["fee"] = (
                    risk.taker_fee(taker_filled, plan["price"], fees.rate(series, self.s))
                    + risk.taker_fee(maker_filled, plan["price"], fees.rate(series, self.s, maker=True))
                )
            except Exception as e:
                self._pause_for_reconciliation(
                    f"single-order submission or fill confirmation uncertain for {snap['ticker']}: {e}"
                )
                log.error("order failed %s: %s", snap["ticker"], e)
                return

        self.store.add_bet({
            "ts": now, "mode": self.s.mode, "ticker": snap["ticker"],
            "event_ticker": snap["event_ticker"], "series": snap["series"],
            "team": snap["team"], "side": plan["side"], "price": plan["price"],
            "contracts": plan["contracts"], "stake": plan["stake"], "fee": plan["fee"],
            "model_prob": prob, "edge": plan["edge"], "confidence": conf,
            "signals": {**{r.name: {"p": round(r.prob, 4), "c": round(r.confidence, 3),
                                    **{k: v for k, v in r.detail.items()
                                       if k in ("reason", "players", "kalshi_leads_books")}}
                           for r in readings},
                        **({"_fill_edge": plan["fill_edge"]} if "fill_edge" in plan else {})},
            "order_id": order_id, "style": plan.get("style", "taker")})
        summary["bets"] += 1
        alerts.send(f"Bet ({self.s.mode}) {snap['ticker']}",
                    f"{plan['side'].upper()} x{plan['contracts']} @ ${plan['price']:.2f} | "
                    f"edge {plan['edge']:+.1%} | confidence {conf:.0%}", tags=["chart_with_upwards_trend"])
        log.info("BET %s %s %s x%d @ $%.2f edge %+.1f%% conf %.0f%%", self.s.mode,
                 snap["ticker"], plan["side"], plan["contracts"], plan["price"],
                 plan["edge"] * 100, conf * 100)
