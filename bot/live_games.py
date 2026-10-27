"""In-game (live) sports betting, PAPER ONLY.

Pre-game bets come from Engine.run. Once a game starts, this loop takes over:
it polls ESPN's free live scoreboards every LIVE_POLL_SECONDS while a scheduled
run is open (the runs chain back to back, so coverage is near-continuous) and
prices each Kalshi game market from the live score:

    live_score_model  P(win) = Phi((lead + m0 * f) / (sigma * sqrt(f)))
                      f = share of the game left, m0 = pre-game expected margin
                      implied by the market's closing price, sigma = how much a
                      full game's margin swings (learned per sport, below)
    espn_live_wp      ESPN's own live win probability, when it publishes one
    kalshi_market     the current mid

Every poll is saved (live_game_snaps). After each game settles the snapshots
are graded, and learn() refits sigma per sport and the weight of each source,
so the in-game model gets sharper after every finished game. A fitted sigma
is used only if it beats the default on the newest held-out games.

Safety: paper mode only; stricter edge (LIVE_MIN_EDGE) and smaller stakes
(LIVE_MAX_PER_BET); no bet on a score older than LIVE_MAX_SCORE_AGE seconds, in
the last 3% of a game, or when the score model and ESPN disagree badly (likely a
data mismatch); one bet per game across pre-game and live (risk.decide).
"""
import logging
import math
import time

from scipy.stats import norm

from . import brain, council, guard, risk
from .config import SPORTS
from .matching import name_score
from .signals.base import Reading, clamp

log = logging.getLogger("bot")

# Kalshi series -> ESPN scoreboard key (bot.sports_intel.ESPN_SCOREBOARDS)
ESPN_FOR_SERIES = {"KXNBAGAME": "nba", "KXNFLGAME": "nfl", "KXMLBGAME": "mlb",
                   "KXNHLGAME": "nhl", "KXNCAAFGAME": "college_football",
                   "KXWNBAGAME": "wnba"}
# regulation periods, minutes per period (None = innings), default full-game margin sd
GAME = {"nba": (4, 12, 13.0), "wnba": (4, 10, 11.0), "nfl": (4, 15, 13.5),
        "college_football": (4, 15, 16.0), "nhl": (3, 20, 2.3), "mlb": (9, None, 4.4)}
SIGMA_GRID = tuple(round(0.6 + 0.05 * i, 2) for i in range(21))     # x default, 0.60-1.60
MIN_LEARN_GAMES = 30
END_OF_GAME = 0.03            # no bets in the last 3% of a game
MAX_DISAGREE = 0.15           # score model vs ESPN: bigger gap = probably bad data
SNAP_EVERY = 60               # seconds between saved snapshots per market

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_game_snaps (
    ticker TEXT NOT NULL, ts INTEGER NOT NULL, series TEXT, sport TEXT,
    espn_event TEXT, is_home INTEGER, lead REAL, frac_left REAL, prior REAL,
    model_p REAL, espn_p REAL, market_p REAL, outcome INTEGER, settled_ts INTEGER,
    PRIMARY KEY (ticker, ts)
);
CREATE INDEX IF NOT EXISTS ix_live_snaps_grade ON live_game_snaps(outcome, sport);
"""


def ensure_table(store):
    with store.lock:
        store.db.executescript(SCHEMA)
        store.db.commit()


# --- the live win model ----------------------------------------------------------
def _clock_seconds(clock):
    try:
        parts = [float(p) for p in str(clock or "").split(":")]
    except ValueError:
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs if parts else None


def fraction_left(sport, period, clock=None, detail=""):
    """Share of regulation left (0-1). Overtime/extra innings count as a sliver."""
    if sport not in GAME:
        return None
    periods, minutes, _ = GAME[sport]
    try:
        period = int(period)
    except (TypeError, ValueError):
        return None
    if period < 1:
        return 1.0
    if minutes is None:                           # baseball: half-innings left
        d = str(detail or "").lower()
        halves = 2 * (period - 1) + (1 if d.startswith(("bot", "mid")) else
                                     2 if d.startswith("end") else 0)
        if period > periods:
            return 0.5 / (2 * periods)
        return max(0.0, (2 * periods - halves) / (2 * periods))
    secs = _clock_seconds(clock)
    if secs is None:
        return None
    total = periods * minutes * 60
    if period > periods:                          # overtime
        return max(secs / total, 0.002)
    return max(0.0, min(1.0, ((periods - period) * minutes * 60 + secs) / total))


def win_prob(lead, frac, prior, sigma):
    """P(this team wins) given its current lead, the share of game left, its
    pre-game win probability, and the full-game margin sd."""
    prior = min(max(float(prior), 0.01), 0.99)
    if frac <= 0:
        return 1.0 if lead > 0 else 0.0 if lead < 0 else 0.5
    m0 = sigma * norm.ppf(prior)
    return float(norm.cdf((lead + m0 * frac) / (sigma * math.sqrt(frac))))


def sigma_for(store, sport):
    fit = store.get_kv(f"live_sigma_{sport}") or {}
    default = GAME[sport][2]
    if fit.get("accepted") and isinstance(fit.get("sigma"), (int, float)) and \
            0.5 * default <= fit["sigma"] <= 2 * default:
        return float(fit["sigma"])
    return default


def source_weights(store):
    """Blend weights from each source's graded Brier (better = heavier)."""
    w = store.get_kv("live_source_weights") or {}
    return {"live_score_model": float(w.get("live_score_model", 1.0)),
            "espn_live_wp": float(w.get("espn_live_wp", 1.0)),
            "kalshi_market": float(w.get("kalshi_market", 1.0))}


# --- ESPN live state -------------------------------------------------------------
def _fetch_json(url):
    from .sports_intel import _request
    return _request(url).json()


def scoreboard(sport, fetch=None, now=None):
    """Live events for one ESPN league: [{id, state, period, clock, detail, competitors}]."""
    from .sports_intel import ESPN_SCOREBOARDS, _parse_espn
    league = ESPN_SCOREBOARDS.get(sport)
    if not league:
        return []
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league[0]}/{league[1]}/scoreboard"
    payload = (fetch or _fetch_json)(url)
    return _parse_espn(payload, "ESPN scoreboard", sport, int(now or time.time()))


def espn_win_prob(sport, event_id, fetch=None):
    """ESPN's latest live home-win probability, or None."""
    from .sports_intel import ESPN_SCOREBOARDS
    league = ESPN_SCOREBOARDS.get(sport)
    if not league or not event_id:
        return None
    url = (f"https://site.api.espn.com/apis/site/v2/sports/{league[0]}/{league[1]}/summary"
           f"?event={event_id}")
    try:
        wp = (fetch or _fetch_json)(url).get("winprobability") or []
        p = float(wp[-1]["homeWinPercentage"])
    except Exception:
        return None
    return p if 0 <= p <= 1 else None


def match_team(team_name, team_code, event):
    """The ESPN competitor this Kalshi market's team is, or None."""
    best, score = None, 0.0
    for c in event.get("competitors") or []:
        sc = name_score(team_name or "", c.get("name") or "")
        if team_code and str(c.get("abbreviation") or "").upper() == team_code.upper():
            sc = max(sc, 0.95)
        if sc > score:
            best, score = c, sc
    return best if score >= 0.75 else None


def same_game(kalshi_event, espn_event, hours=6):
    """Kalshi lists days of games; a team playing tonight must not be matched to
    its next game. Both start times must be known and within `hours`."""
    from .engine import iso_to_ts
    from .event_clock import event_start_ts
    k, e = event_start_ts(kalshi_event), iso_to_ts(espn_event.get("start_time") or "")
    return bool(k and e and abs(k - e) <= hours * 3600)


def _score(c):
    try:
        return float(c.get("score"))
    except (TypeError, ValueError):
        return None


# --- the loop ----------------------------------------------------------------------
class LiveGames:
    def __init__(self, settings, store, engine, fetch=None, now_fn=time.time):
        self.s, self.store, self.eng = settings, store, engine
        self.fetch, self.now = fetch, now_fn
        self.last_snap = {}
        self.espn_cache = {}
        ensure_table(store)

    def leagues(self):
        selected = self.s.selected_series()
        return {series: sport for series, sport in ESPN_FOR_SERIES.items()
                if series in SPORTS and (selected is None or series in selected)}

    def live_events(self):
        """{series: [(espn_event, sport)]} for games in progress right now."""
        out = {}
        for series, sport in self.leagues().items():
            try:
                events = [e for e in scoreboard(sport, self.fetch, self.now())
                          if e.get("state") == "in"]
            except Exception as e:
                log.info("live scoreboard %s: %s", sport, e)
                continue
            if events:
                out[series] = [(e, sport) for e in events]
        return out

    def _espn_p(self, sport, event_id, now):
        hit = self.espn_cache.get(event_id)
        if hit and now - hit[0] < 60:
            return hit[1]
        p = espn_win_prob(sport, event_id, self.fetch)
        self.espn_cache[event_id] = (now, p)
        return p

    def price_market(self, m, ev, sport, espn_ev, now):
        """(readings, detail) for one Kalshi team market in a live game, or (None, why)."""
        team = m.get("yes_sub_title") or m.get("subtitle")
        code = str(m.get("ticker", "")).rsplit("-", 1)[-1]
        me = match_team(team, code, espn_ev)
        if not me:
            return None, "team not matched to live game"
        opp = next((c for c in espn_ev["competitors"] if c is not me), None)
        mine, theirs = _score(me), _score(opp or {})
        frac = fraction_left(sport, espn_ev.get("period"), espn_ev.get("clock"),
                             espn_ev.get("detail"))
        if mine is None or theirs is None or frac is None:
            return None, "live score incomplete"
        from .grader import closing_yes
        start = self.store.get_kv(f"start_{m['ticker']}")
        if not start:
            from .engine import iso_to_ts
            start = iso_to_ts(espn_ev.get("start_time"))
        prior = closing_yes(self.store, m["ticker"], start) if start else None
        if prior is None or not 0 < prior < 1:
            return None, "no pre-game price to anchor on"
        is_home = me.get("home_away") == "home"
        model_p = win_prob(mine - theirs, frac, prior, sigma_for(self.store, sport))
        home_p = self._espn_p(sport, espn_ev.get("provider_event_id"), now)
        espn_p = None if home_p is None else (home_p if is_home else 1 - home_p)
        bid, ask = m.get("yes_bid"), m.get("yes_ask")
        mid = (bid + ask) / 2 if bid is not None and ask else None
        w = source_weights(self.store)
        readings = [Reading("live_score_model", clamp(model_p, 0.01, 0.99), 0.85,
                            {"lead": mine - theirs, "frac_left": round(frac, 4),
                             "prior": prior, "weight": w["live_score_model"]})]
        if espn_p is not None:
            readings.append(Reading("espn_live_wp", clamp(espn_p, 0.01, 0.99), 0.75,
                                    {"weight": w["espn_live_wp"]}))
        if mid is not None:
            readings.append(Reading("kalshi_market", clamp(mid, 0.01, 0.99), 0.6,
                                    {"mid": mid, "weight": w["kalshi_market"]}))
        detail = {"is_home": is_home, "lead": mine - theirs, "frac": frac, "prior": prior,
                  "model_p": model_p, "espn_p": espn_p, "market_p": mid}
        return readings, detail

    def save_snap(self, m, series, sport, espn_ev, d, now):
        if now - self.last_snap.get(m["ticker"], 0) < SNAP_EVERY:
            return
        self.last_snap[m["ticker"]] = now
        self.store.x("""INSERT OR IGNORE INTO live_game_snaps (ticker, ts, series, sport,
                        espn_event, is_home, lead, frac_left, prior, model_p, espn_p, market_p)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (m["ticker"], int(now), series, sport, espn_ev.get("provider_event_id"),
                      int(d["is_home"]), d["lead"], d["frac"], d["prior"], d["model_p"],
                      d["espn_p"], d["market_p"]))

    def max_bet(self):
        """Live stake cap, lifted above the minimum bet (fees count against the
        cap, so a cap equal to the minimum could never fit a bet)."""
        floor = float(getattr(self.s, "min_per_bet", 0) or 0) * 1.1
        return max(float(self.s.live_max_per_bet), floor)

    def blend(self, readings):
        weights = {r.name: r.detail.get("weight", 1.0) for r in readings}
        total = sum(weights[r.name] * r.confidence for r in readings)
        prob = sum(weights[r.name] * r.confidence * r.prob for r in readings) / total
        conf = max(r.confidence for r in readings if r.name != "kalshi_market")
        return prob, conf

    def consider(self, ev, m, series, sport, espn_ev, summary, now):
        snap = self.eng.snapshot(ev, m, now)
        readings, d = self.price_market(m, ev, sport, espn_ev, now)
        if readings is None:
            summary["skipped"][d] = summary["skipped"].get(d, 0) + 1
            return
        self.save_snap(m, series, sport, espn_ev, d, now)
        summary["priced"] += 1
        why = None
        if now - int(espn_ev.get("observed_ts") or 0) > self.s.live_max_score_age:
            why = "live score too old"
        elif d["frac"] < END_OF_GAME:
            why = "final moments of the game"
        elif d["espn_p"] is not None and abs(d["model_p"] - d["espn_p"]) > MAX_DISAGREE:
            why = "score model and ESPN disagree (data check)"
        elif not self.eng.betting_ok or guard.league_benched(self.store, series, mode=self.s.mode):
            why = "betting paused"
        if why:
            summary["skipped"][why] = summary["skipped"].get(why, 0) + 1
            return
        prob, conf = self.blend(readings)
        try:
            book = self.eng.data.orderbook(m["ticker"])
        except Exception as e:
            summary["skipped"]["orderbook unavailable"] = summary["skipped"].get("orderbook unavailable", 0) + 1
            log.info("live orderbook %s: %s", m["ticker"], e)
            return
        max_bet = self.max_bet()
        plan, why = risk.decide(self.s, self.store, snap, prob, conf, book,
                                min_edge=self.s.live_min_edge, max_per_bet=max_bet)
        if not plan:
            self.eng._record_decision(snap, readings, "rejected", f"live: {why}", prob, conf, now=now)
            summary["skipped"][why] = summary["skipped"].get(why, 0) + 1
            return
        plan["execution_min_edge"] = self.s.live_min_edge
        plan["execution_max_per_bet"] = max_bet
        verdict = council.convene(self.store, self.s, snap, plan, readings, prob, conf, now=now)
        if verdict.get("binding") and verdict.get("decision") == "pass":
            self.eng._record_decision(snap, readings, "rejected", verdict["reason"], prob, conf,
                                      side=plan["side"], market_price=plan["price"],
                                      edge=plan["edge"], now=now)
            return
        self.eng._record_decision(snap, readings, "candidate", "live: passed risk checks", prob,
                                  conf, side=plan["side"], market_price=plan["price"],
                                  edge=plan["edge"], now=now)
        before = summary.get("bets", 0)
        self.eng.place(snap, plan, readings, prob, conf, summary=summary, now=int(now))
        if summary.get("bets", 0) > before:
            summary["live_bets"] += 1

    def run(self, minutes, sleep=time.sleep):
        summary = {"live_games": 0, "matched": 0, "priced": 0, "bets": 0, "live_bets": 0,
                   "polls": 0, "skipped": {}}
        if self.s.mode != "paper" or not self.s.live_games_enabled:
            summary["skipped"]["live game bets are paper-only" if self.s.mode != "paper"
                               else "LIVE_GAMES_ENABLED=no"] = 1
            return self._done(summary)
        deadline = self.now() + minutes * 60
        markets = {}                                   # series -> Kalshi events
        while True:
            now = self.now()
            live = self.live_events()
            summary["polls"] += 1
            summary["live_games"] = max(summary["live_games"], sum(len(v) for v in live.values()))
            if not live:
                break                                 # nothing in progress: free the run
            for series, games in live.items():
                if series not in markets:
                    try:
                        markets[series] = self.eng.data.sports_events(series)
                    except Exception as e:
                        log.info("live kalshi events %s: %s", series, e)
                        markets[series] = []
                for espn_ev, sport in games:
                    for ev in markets[series]:
                        ev.setdefault("series_ticker", series)
                        if not same_game(ev, espn_ev):
                            continue
                        open_m = [m for m in ev.get("markets", [])
                                  if m.get("status") in ("active", "open")]
                        if not any(match_team(m.get("yes_sub_title"),
                                              str(m.get("ticker", "")).rsplit("-", 1)[-1], espn_ev)
                                   for m in open_m):
                            continue
                        summary["matched"] += 1
                        for m in open_m:
                            try:
                                self.consider(ev, m, series, sport, espn_ev, summary, now)
                            except Exception as e:      # one bad market never stops the loop
                                log.warning("live game %s: %s", m.get("ticker"), e)
                        break
            if self.now() + self.s.live_poll_seconds >= deadline:
                break
            sleep(self.s.live_poll_seconds)
            markets = {s: self._refresh(s) for s in markets}   # fresh quotes each poll
        return self._done(summary)

    def _refresh(self, series):
        try:
            return self.eng.data.sports_events(series)
        except Exception:
            return []

    def _done(self, summary):
        self.store.set_kv("live_games_health", {"ts": int(self.now()), **summary})
        return summary


def run(settings, store, minutes, data=None, fetch=None):
    from .engine import Engine
    eng = Engine(settings, store, data_client=data, llm=False)
    eng.betting_ok, _ = guard.status(store, settings.bankroll, mode=settings.mode)
    return LiveGames(settings, store, eng, fetch=fetch).run(minutes)


# --- grading + fast learning -----------------------------------------------------
def grade(store, client, max_fetch=60, now=None):
    """Label snapshots of settled games with the team's real result."""
    ensure_table(store)
    tickers = [r["ticker"] for r in store.q(
        "SELECT DISTINCT ticker FROM live_game_snaps WHERE outcome IS NULL LIMIT ?", (max_fetch,))]
    done = 0
    for t in tickers:
        try:
            result = client.market(t).get("result")
        except Exception:
            continue
        if result in ("yes", "no"):
            store.x("UPDATE live_game_snaps SET outcome=?, settled_ts=? WHERE ticker=? AND outcome IS NULL",
                    (1 if result == "yes" else 0, int(now or time.time()), t))
            done += 1
    return done


def _logloss(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def learn(store, now=None):
    """Refit sigma per sport and the source weights from graded live snapshots.
    Runs every scheduled cycle, so each finished game teaches the next one."""
    ensure_table(store)
    now = int(now or time.time())
    out = {}
    for sport, (_, _, default) in GAME.items():
        rows = store.q("""SELECT ticker, ts, lead, frac_left, prior, outcome FROM live_game_snaps
                          WHERE sport=? AND outcome IS NOT NULL ORDER BY ts""", (sport,))
        first = {}
        for r in rows:                                # rows are time-ordered
            first.setdefault(r["ticker"], r["ts"])
        games = sorted(first, key=first.get)
        fit = {"ts": now, "sport": sport, "games": len(games), "accepted": False,
               "default": default}
        if len(games) < MIN_LEARN_GAMES:
            fit["reason"] = f"not enough graded games ({len(games)} < {MIN_LEARN_GAMES})"
        else:
            cut = set(games[int(len(games) * 0.7):])
            train = [r for r in rows if r["ticker"] not in cut]
            test = [r for r in rows if r["ticker"] in cut]

            def loss(rs, sigma):
                return sum(_logloss(win_prob(r["lead"], r["frac_left"], r["prior"], sigma),
                                    r["outcome"]) for r in rs) / max(len(rs), 1)
            best = min(SIGMA_GRID, key=lambda k: loss(train, default * k))
            cand, base = loss(test, default * best), loss(test, default)
            fit.update({"sigma": round(default * best, 3), "holdout_games": len(cut),
                        "holdout_logloss": round(cand, 5), "holdout_logloss_default": round(base, 5)})
            fit["accepted"] = cand < base
            fit["reason"] = "beat the default on newer games" if fit["accepted"] else \
                "did not beat the default on newer games"
        store.set_kv(f"live_sigma_{sport}", fit)
        out[sport] = fit
    # source weights: inverse Brier over all graded snapshots, team-side
    rows = store.q("""SELECT model_p, espn_p, market_p, outcome FROM live_game_snaps
                      WHERE outcome IS NOT NULL""")
    weights = {}
    for name, col in (("live_score_model", "model_p"), ("espn_live_wp", "espn_p"),
                      ("kalshi_market", "market_p")):
        scored = [(r[col] - r["outcome"]) ** 2 for r in rows if r[col] is not None]
        if len(scored) >= 200:
            weights[name] = round(min(4.0, 0.25 / max(sum(scored) / len(scored), 0.02)), 3)
    if weights:
        store.set_kv("live_source_weights", dict(weights, ts=now))
    out["weights"] = weights
    return out
