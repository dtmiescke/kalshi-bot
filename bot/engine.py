"""One research-and-trade cycle. Safe to run every 5-15 minutes."""
import json
import os
import logging
import time
import uuid
from datetime import datetime

from . import alerts, brain, guard, risk
from .llm import LLM
from .config import CRYPTO_15M, SPORTS
from .kalshi_client import KalshiClient, num, price
from .matching import match_event
from .odds import OddsClient, consensus, odds_due
from . import crypto_brain
from .live import settlement
from .signals import crypto as crypto_sig
from .signals import implied_vol as iv
from .signals import flow as flow_sig
from .signals import intel
from .signals import market_signals as ms
from .signals import schedule as sched

log = logging.getLogger("bot")


def _series_sport(series, metadata=None):
    """Map a discovered Kalshi series to a known odds key or a research category."""
    if series in SPORTS:
        return SPORTS[series]
    meta = metadata or {}
    text = " ".join([str(series), str(meta.get("title", "")), str(meta.get("category", "")),
                     " ".join(str(v) for v in (meta.get("tags") or []))]).lower()
    rules = (
        (("nfl", "college football", "football"), "football"),
        (("basketball", "wnba", "nba", "ncaab"), "basketball"),
        (("baseball", "mlb"), "baseball"),
        (("hockey", "nhl"), "hockey"),
        (("soccer", "premier league", "champions league", "mls"), "soccer"),
        (("tennis", "atp", "wta"), "tennis"),
        (("mma", "ufc", "mixed martial", "boxing"), "mma"),
        (("golf", "pga", "lpga"), "golf"),
        (("formula 1", "formula-one", "motorsport", "nascar", "racing"), "motorsports"),
        (("esports", "e-sports", "league of legends", "counter-strike", "dota"), "esports"),
        (("cricket",), "cricket"),
        (("rugby",), "rugby"),
        (("volleyball",), "volleyball"),
        (("chess",), "chess"),
    )
    for words, category in rules:
        if any(word in text for word in words):
            return category
    return "other"


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
        import os as _os
        self.odds_max_age = float(_os.environ.get("ODDS_MAX_AGE_MINUTES", 60))
        self.price_feed = price_feed or crypto_sig.fetch_candles
        self.spot_feed = spot_feed or crypto_sig.fetch_spot
        self.llm = llm if llm is not None else (
            LLM(settings.openrouter_key, store) if settings.ai_enabled() else None)
        self.schedule_fetch = schedule_fetch or sched.fetch_games
        self.public = ms.load_public_betting()
        self.betting_ok = True
        self._headlines = {}

    # --- data collection -------------------------------------------------------
    def refresh_odds(self, sport):
        """Pull books when the credit budget allows, store vig-free consensus."""
        # Sports scans run every few hours, so always refresh lines older than
        # ODDS_MAX_AGE_MINUTES: comparing a live Kalshi price with a stale
        # sportsbook line manufactures fake edges.
        if not odds_due(self.store, sport, min(self.s.odds_refresh_minutes, self.odds_max_age)):
            return
        events = self.odds.fetch(sport, self.s.odds_regions)
        now = int(time.time())
        slim = []
        for ev in events:
            for team, (p, n, spread) in consensus(ev).items():
                self.store.add_odds({
                    "ts": now, "sport": sport, "event_id": ev["id"],
                    "commence_time": ev.get("commence_time"), "home": ev.get("home_team"),
                    "away": ev.get("away_team"), "team": team, "fair_prob": p,
                    "n_books": n, "raw": {"spread": spread}})
            slim.append({k: ev.get(k) for k in ("id", "home_team", "away_team", "commence_time")})
        if events:
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
        if intel.news_due(self.store, mkt["ticker"]):
            if sport not in self._headlines:
                self._headlines[sport] = intel.fetch_headlines(sport)
                # Merge the free-first scheduled web collector into the existing
                # event-specific news analysis. Collected claims remain unverified
                # evidence and cannot independently authorize a trade.
                try:
                    from . import sports_intel
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
                    category = category_for.get(sport, sport if sport in sports_intel.SPORT_TERMS else None)
                    now_ts = int(time.time())
                    categories = [category] if category else []
                    categories.append("event_news")
                    for news_category in set(categories):
                        for item in sports_intel.recent(self.store, limit=250, category=news_category, kind="news"):
                            published = item.get("published_ts")
                            if published and now_ts - published > 48 * 3600:
                                continue
                            item_event = item.get("event_ticker")
                            if item_event and mkt.get("event_ticker") and item_event != mkt.get("event_ticker"):
                                continue
                            self._headlines[sport].append({
                                "headline": f"[{item.get('publisher') or item.get('source') or 'unknown source'}] {item.get('title', '')}",
                                "description": item.get("summary", ""),
                                "published": published or item.get("observed_ts"),
                                "source": item.get("source", ""),
                                "url": item.get("url", ""),
                            })
                    # Include recent live/final scoreboard and box-score evidence
                    # for these teams, without treating an unverified feed as truth.
                    stat_categories = {
                        "americanfootball_nfl": ["nfl"], "americanfootball_ncaaf": ["college_football"],
                        "basketball_nba": ["nba"], "basketball_wnba": ["wnba"],
                        "baseball_mlb": ["mlb"], "icehockey_nhl": ["nhl"],
                        "soccer_epl": ["soccer_epl"], "soccer_mls": ["soccer_mls"],
                        "soccer_uefa": ["soccer_uefa"], "tennis_atp": ["tennis_atp"],
                        "tennis_wta": ["tennis_wta"], "golf_pga": ["golf_pga"],
                        "mma_mixed_martial_arts": ["mma_ufc"],
                    }
                    stat_rows = []
                    team_terms = [str(x).lower() for x in (team, opp) if x and len(str(x)) > 2]
                    for stat_category in stat_categories.get(sport, []):
                        for item in sports_intel.recent(self.store, limit=30, category=stat_category, kind="stats"):
                            serialized = json.dumps(item, ensure_ascii=False).lower()
                            if team_terms and not any(term in serialized for term in team_terms):
                                continue
                            stat_rows.append({
                                "event": item.get("title"), "observed_ts": item.get("observed_ts"),
                                "state": item.get("state"), "scoreboard": item.get("scoreboard", []),
                                "team_stats": item.get("team_stats", []),
                                "leaders": item.get("leaders", []),
                                "public_odds": item.get("odds", []),
                                "predictor": item.get("predictor"),
                            })
                    if stat_rows:
                        context = dict(context or {})
                        context["recent_scoreboard_stats"] = stat_rows[:3]
                except Exception as e:
                    log.debug("cached web intelligence unavailable for %s: %s", sport, e)
            heads = intel.relevant(self._headlines[sport], [team, opp or ""])
            book = hist[-1]["fair_prob"] if hist else None
            r = intel.ai_news(self.llm, team, opp, mid, book, heads, context)
            self.store.set_kv(f"news_last_{mkt['ticker']}", time.time())
            self.store.set_kv(key, r.__dict__ if r else None)
        cached = self.store.get_kv(key)
        return intel.Reading(**cached) if cached else None

    # --- the cycle ---------------------------------------------------------------
    def run(self, watch=None, sports=True):
        """watch: optional Watchlist. When given, only watched games (plus games
        the bot already holds) are researched — a fast spot check."""
        self.s.validate()
        now = int(time.time())
        summary = {"mode": "watch" if watch else "full", "markets": 0,
                   "matched": 0, "bets": 0, "skipped": {}, "alerts": []}
        held = {b["event_ticker"] for b in self.store.open_bets()}
        if sports and getattr(self.s, "combos_enabled", False):
            from . import combo_research
            summary["combos"] = combo_research.scan(self.data, self.store)
        weights = brain.get_weights(self.store, "sports")
        self.betting_ok, why = guard.status(self.store, self.s.bankroll)
        summary["betting"] = why
        if not self.betting_ok:
            log.warning("betting paused (%s): researching and learning only", why)
            if self.store.get_kv("pause_alerted") != why:
                alerts.send("Betting paused", why, priority="high", tags=["warning"])
                self.store.set_kv("pause_alerted", why)
        else:
            self.store.set_kv("pause_alerted", None)

        series_map = dict(SPORTS)
        events_by_series = {}
        selected = self.s.selected_series() if hasattr(self.s, "selected_series") else None
        if sports and getattr(self.s, "sports_mode", "all") == "all":
            try:
                catalog = self.data.sports_series()
                all_events = self.data.sports_events_all()
                for ev in all_events:
                    series = ev.get("series_ticker")
                    if not series:
                        event_ticker = str(ev.get("event_ticker") or "")
                        series = next((known for known in catalog if event_ticker.startswith(known + "-")), None)
                    if not series or series not in catalog:
                        continue
                    series_map[series] = _series_sport(series, catalog.get(series))
                    events_by_series.setdefault(series, []).append(ev)
                self.store.set_kv("sports_catalog_health", {
                    "ts": now, "series_discovered": len(catalog),
                    "open_events_seen": len(all_events), "sports_events_matched": sum(map(len, events_by_series.values())),
                    "coverage_warning": len(all_events) >= 4000 or len(catalog) >= 10000,
                    "mode": "dynamic_all"})
            except Exception as e:
                log.warning("dynamic sports discovery failed; using configured series: %s", e)
                events_by_series = {}
        elif sports and getattr(self.s, "sports_mode", "all") == "manual":
            selected = selected or set()
            series_map = {series: SPORTS.get(series, _series_sport(series))
                          for series in selected}
            self.store.set_kv("sports_catalog_health", {
                "ts": now, "series_selected": sorted(selected), "mode": "manual"})
        else:
            series_map = {}

        if sports and not events_by_series and series_map:
            for selected_series in series_map:
                try:
                    events_by_series[selected_series] = self.data.sports_events(selected_series)
                except Exception as e:
                    log.warning("events %s: %s", selected_series, e)
                    events_by_series[selected_series] = []
        if sports and events_by_series and os.environ.get("SPORTS_INTEL_EVENT_SEARCH", "yes").lower() in ("yes", "true", "1"):
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
                    self.refresh_odds(sport)
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
                benched = guard.league_benched(self.store, series)
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
                    c = self.evaluate(snap, readings, weights, summary)
                    if c:
                        candidates.append(c)
                if candidates:
                    best = max(candidates, key=lambda c: c[1]["edge"])
                    if watch:
                        summary["alerts"].append(
                            f"{best[0]['ticker']} {best[1]['side'].upper()} @ "
                            f"${best[1]['price']:.2f}, edge {best[1]['edge']:+.1%}")
                    self.place(*best, summary=summary, now=now)
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
                    if not self.betting_ok or guard.league_benched(self.store, series):
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
                            guard.league_benched(self.store, series):
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

    def execute_ladder(self, a, now, summary):
        """Re-check both books, then buy YES(low) + NO(high) in equal size."""
        try:
            b_low, b_high = self.data.orderbook(a["low"]), self.data.orderbook(a["high"])
        except Exception:
            return
        yes_low = risk.asks_from_book(b_low)["yes"]
        no_high = risk.asks_from_book(b_high)["no"]
        if not yes_low or not no_high:
            return
        (pl, ql), (ph, qh) = yes_low[0], no_high[0]
        left_day = self.s.max_per_day - self.store.staked_since(risk.day_start_ts())
        n = int(min(ql, qh, min(self.s.max_per_game, left_day) // (pl + ph)))
        fees = risk.taker_fee(n, pl, self.s.taker_fee_rate) + risk.taker_fee(n, ph, self.s.taker_fee_rate)
        if n < 1 or n * (1 - pl - ph) - fees <= 0:
            return
        placed = []   # place BOTH legs first; record only a complete arbitrage
        for ticker, side, px in ((a["low"], "yes", pl), (a["high"], "no", ph)):
            oid = f"paper-ladder-{uuid.uuid4().hex[:8]}"
            if self.s.mode in ("demo", "live"):
                try:
                    o = self.trade.place_limit_buy(ticker, side, n, px)
                    oid = o.get("order_id", "")
                    if int(num(o, "fill_count") or num(o, "taker_fill_count") or 0) < n:
                        raise RuntimeError("leg not fully filled")
                except Exception as e:
                    log.error("LADDER LEG FAILED %s: %s", ticker, e)
                    for t, sd, _, _ in placed:          # sell back what did fill
                        try:
                            bid = risk.bid_for(self.data.orderbook(t), sd)
                            if bid:
                                self.trade.place_limit_sell(t, sd, n, bid)
                        except Exception as e2:
                            log.error("UNWIND FAILED %s: %s", t, e2)
                            alerts.send("Unwind failed", f"{t}: check manually", priority="urgent")
                    return
            placed.append((ticker, side, px, oid))
        for ticker, side, px, oid in placed:
            self.store.add_bet({
                "ts": now, "mode": self.s.mode, "ticker": ticker, "event_ticker": ticker,
                "series": "ARB", "team": None, "side": side, "price": px, "contracts": n,
                "stake": round(n * px, 2), "fee": risk.taker_fee(n, px, self.s.taker_fee_rate),
                "model_prob": None, "edge": None, "confidence": 1.0,
                "signals": {"arbitrage": {"pair": [a["low"], a["high"]],
                                          "locked": round(n * (1 - pl - ph) - fees, 2)}},
                "order_id": oid, "style": "taker"})
        summary["bets"] += 2

    def try_arb(self, ev, markets, summary, now):
        """Buy YES on both teams when together they cost < $1 after fees."""
        if self.store.has_open_event_bet(ev["event_ticker"]):
            return False
        try:
            books = [self.data.orderbook(m["ticker"]) for m in markets]
        except Exception:
            return False
        left_day = self.s.max_per_day - self.store.staked_since(risk.day_start_ts())
        plan = guard.arb_plan(ev["series_ticker"], books[0], books[1],
                              self.s.taker_fee_rate, min(self.s.max_per_game, left_day))
        if not plan:
            return False
        legs = [(markets[0], plan["price_a"]), (markets[1], plan["price_b"])]
        order_ids = []
        for m, px in legs:
            oid = f"paper-arb-{uuid.uuid4().hex[:8]}"
            if self.s.mode in ("demo", "live"):
                try:
                    o = self.trade.place_limit_buy(m["ticker"], "yes", plan["contracts"], px)
                    oid = o.get("order_id", "")
                    if int(num(o, "fill_count") or num(o, "taker_fill_count") or 0) < plan["contracts"]:
                        raise RuntimeError("leg not fully filled")
                except Exception as e:
                    log.error("ARB LEG FAILED %s: %s", m["ticker"], e)
                    self.unwind(order_ids, plan["contracts"])
                    return False
            order_ids.append((m, px, oid))
        for m, px, oid in order_ids:
            fee = risk.taker_fee(plan["contracts"], px, self.s.taker_fee_rate)
            self.store.add_bet({
                "ts": now, "mode": self.s.mode, "ticker": m["ticker"],
                "event_ticker": ev["event_ticker"], "series": ev["series_ticker"],
                "team": m.get("yes_sub_title"), "side": "yes", "price": px,
                "contracts": plan["contracts"], "stake": round(plan["contracts"] * px, 2),
                "fee": fee, "model_prob": None, "edge": None, "confidence": 1.0,
                "signals": {"arbitrage": {"locked_profit": plan["locked_profit"]}},
                "order_id": oid})
        summary["bets"] += len(order_ids)
        summary.setdefault("arbs", []).append(
            f"{ev['event_ticker']}: locked ${plan['locked_profit']:.2f} on ${plan['stake']:.2f}")
        log.info("ARB %s locked $%.2f", ev["event_ticker"], plan["locked_profit"])
        return True

    def unwind(self, legs, contracts):
        """A half-built arbitrage is just an unhedged bet. Sell back any leg that
        already filled, immediately, at whatever the market bids."""
        for m, px, _ in legs:
            try:
                book = self.data.orderbook(m["ticker"])
                bid = risk.bid_for(book, "yes")
                if bid:
                    self.trade.place_limit_sell(m["ticker"], "yes", contracts, bid)
                    log.warning("unwound arb leg %s at $%.2f", m["ticker"], bid)
            except Exception as e:
                log.error("UNWIND FAILED %s: %s — check this position manually", m["ticker"], e)
                alerts.send("Unwind failed", f"{m['ticker']}: check manually", priority="urgent")

    def _skip(self, summary, reason):
        # group by the reason's wording, without the numbers
        key = " ".join(w for w in reason.split() if not any(c.isdigit() for c in w))[:40]
        summary["skipped"][key] = summary["skipped"].get(key, 0) + 1

    def evaluate(self, snap, readings, weights, summary, required="book_consensus",
                 min_edge=None, max_per_bet=None):
        if not any(r.name == required for r in readings):
            self._skip(summary, "no outside signal")
            return None
        bc = next((r for r in readings if r.name == "book_consensus"), None)
        if bc and bc.detail.get("age_min", 0) > self.odds_max_age * 1.5:
            self._skip(summary, "sportsbook odds too old")
            return None
        prob, conf = brain.blend(readings, weights)
        if prob is None:
            return None
        try:
            book = self.data.orderbook(snap["ticker"])
        except Exception as e:
            log.warning("orderbook %s: %s", snap["ticker"], e)
            return None
        plan, why = risk.decide(self.s, self.store, snap, prob, conf, book,
                                min_edge=min_edge, max_per_bet=max_per_bet)
        if not plan:
            self._skip(summary, why)
            return None
        return snap, plan, readings, prob, conf

    def place(self, snap, plan, readings, prob, conf, summary, now, executed=None):
        """Send (or simulate) the order and record the bet. `executed` = an order
        the live runner already filled as a resting maker order."""
        order_id = f"paper-{uuid.uuid4().hex[:10]}"
        if executed:
            order_id = executed.get("order_id", order_id)
            plan["contracts"] = executed.get("contracts", plan["contracts"])
        elif self.s.mode in ("demo", "live"):
            try:
                order = self.trade.place_limit_buy(snap["ticker"], plan["side"],
                                                   plan["contracts"], plan["limit"])
                order_id = order.get("order_id", "")
                filled = int(num(order, "fill_count") or num(order, "taker_fill_count") or 0)
                if filled < 1:
                    self._skip(summary, "order not filled")
                    return
                cost = num(order, "taker_fill_cost") + num(order, "maker_fill_cost")
                if cost > 0:
                    # record what we ACTUALLY paid, not what we planned to pay
                    plan["price"] = round(cost / filled / (100 if cost / filled > 1 else 1), 4)
                plan["contracts"] = filled
                plan["stake"] = round(filled * plan["price"], 2)
            except Exception as e:
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
