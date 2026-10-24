"""Free-first sports intelligence collector.

Uses public RSS and ESPN scoreboard endpoints with caching, timeouts, deduplication,
source-health telemetry and conservative provenance. This module only collects and
stores evidence; it never places or authorizes orders.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)
USER_AGENT = "KalshiIntelligenceBot/1.0 (+public-data; contact repository owner)"
TIMEOUT = (2.0, 4.0)
MAX_BODY = 2_000_000

# These endpoints are public-facing and may change or impose limits. Fail closed.
ESPN_SCOREBOARDS = {
    "nfl": ("football", "nfl"),
    "college_football": ("football", "college-football"),
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "college_basketball": ("basketball", "mens-college-basketball"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "soccer_mls": ("soccer", "usa.1"),
    "soccer_epl": ("soccer", "eng.1"),
    "soccer_uefa": ("soccer", "uefa.champions"),
    "soccer_spain": ("soccer", "esp.1"),
    "soccer_italy": ("soccer", "ita.1"),
    "soccer_germany": ("soccer", "ger.1"),
    "soccer_france": ("soccer", "fra.1"),
    "soccer_netherlands": ("soccer", "ned.1"),
    "soccer_europa": ("soccer", "uefa.europa"),
    "tennis_atp": ("tennis", "atp"),
    "tennis_wta": ("tennis", "wta"),
    "golf_pga": ("golf", "pga"),
    "golf_lpga": ("golf", "lpga"),
    "mma_ufc": ("mma", "ufc"),
    "motorsports_f1": ("racing", "f1"),
    "motorsports_nascar": ("racing", "nascar"),
    "cricket_ipl": ("cricket", "ipl"),
    "rugby_union": ("rugby", "rugby-union"),
}
SPORT_TERMS = {
    "football": "NFL OR college football injury lineup depth chart",
    "basketball": "NBA OR WNBA OR college basketball injury lineup",
    "baseball": "MLB baseball starting pitcher lineup injury",
    "hockey": "NHL hockey goalie lineup injury",
    "soccer": "soccer football lineup injury suspension transfer",
    "tennis": "ATP WTA tennis player injury withdrawal tournament",
    "mma": "UFC MMA fight injury weigh-in result",
    "boxing": "boxing fight injury weigh-in result",
    "golf": "PGA LPGA golf player withdrawal leaderboard",
    "motorsports": "Formula 1 NASCAR driver team race update",
    "cricket": "international cricket IPL BBL lineup injury team news score",
    "rugby": "rugby union league lineup injury team news score",
    "volleyball": "volleyball roster lineup injury tournament score",
    "chess": "chess tournament player updates match result",
    "badminton": "badminton tournament player injury results",
    "cycling": "cycling race rider injury team update results",
    "track_field": "track and field athletics athlete injury results",
    "swimming": "swimming championship athlete results roster",
    "olympics": "Olympic sports athlete injury lineup medal event update",
}
RSS_CATEGORIES = tuple(SPORT_TERMS)

SCOREBOARD_CATEGORIES = {
    "football": {"nfl", "college_football"},
    "basketball": {"nba", "wnba", "college_basketball"},
    "baseball": {"mlb"}, "hockey": {"nhl"},
    "soccer": {key for key in ESPN_SCOREBOARDS if key.startswith("soccer_")},
    "tennis": {"tennis_atp", "tennis_wta"},
    "golf": {"golf_pga", "golf_lpga"}, "mma": {"mma_ufc"},
    "motorsports": {"motorsports_f1", "motorsports_nascar"},
    "cricket": {"cricket_ipl"}, "rugby": {"rugby_union"},
}


def categories_for_series(series_tickers, sports_map, mode="configured"):
    """Map selected Kalshi series to only the matching sports-intelligence feeds."""
    if str(mode).lower() == "all":
        return None
    result = set()
    for series in (series_tickers or ()):
        sport = str(sports_map.get(series, "")).lower()
        ticker = str(series).lower()
        if sport.startswith("americanfootball") or "ncaaf" in ticker or "nfl" in ticker:
            result.add("football")
        elif sport.startswith("basketball") or "nba" in ticker:
            result.add("basketball")
        elif sport.startswith("baseball") or "mlb" in ticker:
            result.add("baseball")
        elif sport.startswith("icehockey") or "nhl" in ticker:
            result.add("hockey")
        elif "soccer" in sport or "soccer" in ticker:
            result.add("soccer")
        elif "tennis" in sport or "tennis" in ticker:
            result.add("tennis")
        elif "golf" in sport or "golf" in ticker:
            result.add("golf")
        elif "mma" in sport or "ufc" in ticker:
            result.add("mma")
        elif "boxing" in sport or "boxing" in ticker:
            result.add("boxing")
        elif "racing" in sport or "motor" in ticker or "f1" in ticker or "nascar" in ticker:
            result.add("motorsports")
        elif "cricket" in sport or "cricket" in ticker:
            result.add("cricket")
        elif "rugby" in sport or "rugby" in ticker:
            result.add("rugby")
        elif sport in ("volleyball", "chess", "badminton", "cycling", "track_field", "swimming", "olympics"):
            result.add(sport)
    return result

def _now() -> int:
    return int(time.time())

def _iso_to_ts(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, OverflowError):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except (ValueError, AttributeError, OverflowError):
            return None

def _clean(value, limit=4000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

def _request(url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/json, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    candidates = [url]
    # ESPN sometimes blocks the site.api host from hosted CI runners. Try its
    # web API host as a fallback, but keep the same path/query and fail closed
    # if neither endpoint returns a valid response.
    if url.startswith("https://site.api.espn.com/"):
        headers["Referer"] = "https://www.espn.com/"
        candidates.append(url.replace("https://site.api.espn.com/",
                                      "https://site.web.api.espn.com/", 1))
    last_error = None
    for candidate in candidates:
        try:
            response = requests.get(candidate, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            if len(response.content) > MAX_BODY:
                raise ValueError("response exceeded size limit")
            return response
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            log.debug("sports source request failed for %s: %s", candidate, exc)
    raise last_error

def _parse_rss(payload, source, category, observed_at):
    root = ET.fromstring(payload)
    rows = []
    # RSS and Atom feeds use different namespaces; local-name matching handles both.
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "item" and node.tag.rsplit("}", 1)[-1].lower() != "entry":
            continue
        fields = {}
        for child in list(node):
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name == "link":
                value = child.attrib.get("href") or child.text
            else:
                value = "".join(child.itertext())
            if name == "source" and child.attrib.get("url"):
                fields["publisher_url"] = child.attrib.get("url")
            if name not in fields and value:
                fields[name] = _clean(value, 8000)
        title = fields.get("title", "")
        link = fields.get("link", "")
        if not title or not link:
            continue
        published = _iso_to_ts(fields.get("pubdate") or fields.get("published") or fields.get("updated") or fields.get("date"))
        description = fields.get("description") or fields.get("summary") or fields.get("content", "")
        canonical = hashlib.sha256((link.strip() + "\n" + title.strip()).encode("utf-8")).hexdigest()
        publisher_url = fields.get("publisher_url", "")
        publisher_host = urlparse(publisher_url).netloc.lower()
        official_hosts = ("nfl.com", "nba.com", "wnba.com", "mlb.com", "nhl.com",
                          "espn.com", "bbc.co.uk", "reuters.com", "apnews.com", "olympics.com")
        is_official_or_major = any(publisher_host == host or publisher_host.endswith("." + host)
                                   for host in official_hosts)
        rows.append({
            "id": canonical, "source": source, "publisher": fields.get("source", ""),
            "publisher_url": publisher_url, "publisher_host": publisher_host,
            "category": category, "title": title, "url": link,
            "summary": re.sub(r"<[^>]+>", " ", description)[:3000],
            "published_ts": published, "observed_ts": observed_at,
            "source_age_seconds": max(0, observed_at - published) if published else None,
            "confidence": (0.6 if is_official_or_major else 0.4) if published else 0.2,
            "kind": "news", "verified": False,
        })
    return rows

def _parse_espn(payload, source, category, observed_at):
    data = payload
    events = []
    for event in data.get("events", []) if isinstance(data, dict) else []:
        comp = (event.get("competitions") or [{}])[0]
        competitors = []
        for team in comp.get("competitors", []):
            competitors.append({
                "name": _clean((team.get("team") or {}).get("displayName") or team.get("displayName")),
                "abbreviation": _clean((team.get("team") or {}).get("abbreviation")),
                "score": _clean(team.get("score")),
                "home_away": team.get("homeAway"),
                "winner": team.get("winner"),
            })
        status = event.get("status") or {}
        status_type = status.get("type") or {}
        odds = comp.get("odds") or event.get("odds") or []
        if not isinstance(odds, list):
            odds = [odds]
        event_row = {
            "id": _clean(event.get("id")), "provider_event_id": _clean(event.get("id")),
            "source": source, "category": category,
            "kind": "score", "name": _clean(event.get("name")),
            "short_name": _clean(event.get("shortName")),
            "start_time": event.get("date"), "observed_ts": observed_at,
            "state": _clean(status_type.get("state")), "status": _clean(status_type.get("description")),
            "detail": _clean(status_type.get("detail")), "clock": _clean(status.get("displayClock")),
            "period": status.get("period"), "competitors": competitors,
            "odds": odds[:10],
            "predictor": event.get("predictor") or comp.get("predictor"),
            "verified": False,
        }
        if event_row["id"] and competitors:
            events.append(event_row)
    return events

def _football_matchup_profile(event, box, raw, observed_at, league_domain):
    """Build a cautious football-specific evidence profile from fields ESPN exposes.

    This is descriptive evidence, not a win-probability model. Missing statistics
    stay missing; provider labels and athlete positions are preserved for audit.
    """
    competitors = event.get("competitors") or []
    teams = []
    for competitor in competitors:
        teams.append({
            "name": competitor.get("name"),
            "abbreviation": competitor.get("abbreviation"),
            "home_away": competitor.get("home_away"),
            "score": competitor.get("score"),
        })

    team_rows = []
    metric_aliases = {
        "total_yards": ("totalyards", "total yards"),
        "passing_yards": ("netpassingyards", "passingyards", "passing yards", "pass yards"),
        "rushing_yards": ("rushingyards", "rushing yards", "rush yards"),
        "turnovers": ("turnovers", "giveaways"),
        "takeaways": ("takeaways", "defensive takeaways"),
        "sacks_allowed": ("sacksallowed", "sacks against"),
        "sacks": ("sacks", "sacks made"),
        "third_down": ("thirddowneff", "third down", "3rd down"),
        "fourth_down": ("fourthdowneff", "fourth down", "4th down"),
        "red_zone": ("redzone", "red zone"),
        "penalties": ("penalties",),
        "time_of_possession": ("possessiontime", "time of possession"),
    }
    for team_block in (box.get("teams") or [])[:4]:
        team_obj = team_block.get("team") or {}
        stats = {}
        for stat in team_block.get("statistics") or []:
            if not isinstance(stat, dict):
                continue
            key = _clean(stat.get("name") or stat.get("label") or stat.get("abbreviation"), 80).lower()
            value = stat.get("displayValue", stat.get("value", stat.get("display")))
            if key and value is not None:
                stats[key] = _clean(value, 80)
        normalized_metrics = {}
        for metric, aliases in metric_aliases.items():
            matches = []
            for key, value in stats.items():
                normalized_key = key.replace("_", "").replace(" ", "").replace("-", "")
                if any(normalized_key == alias.replace("_", "").replace(" ", "").replace("-", "")
                       or (len(alias.replace("_", "").replace(" ", "").replace("-", "")) >= 7
                           and alias.replace("_", "").replace(" ", "").replace("-", "") in normalized_key)
                       for alias in aliases):
                    matches.append((key, value))
            if matches:
                normalized_metrics[metric] = [{"source_label": key, "value": value} for key, value in matches[:3]]
        team_rows.append({
            "team": _clean(team_obj.get("displayName") or team_obj.get("name"), 100),
            "statistics": stats,
            "key_metrics": normalized_metrics,
        })

    player_rows = []
    for group in (box.get("players") or [])[:8]:
        group_team = _clean((group.get("team") or {}).get("displayName"), 100)
        for stat_group in (group.get("statistics") or [])[:8]:
            labels = stat_group.get("labels") or stat_group.get("names") or []
            for athlete_row in (stat_group.get("athletes") or [])[:60]:
                athlete = athlete_row.get("athlete") or {}
                values = athlete_row.get("stats") or []
                stat_values = {}
                for idx, value in enumerate(values):
                    label = labels[idx] if idx < len(labels) else f"stat_{idx + 1}"
                    stat_values[_clean(label, 50)] = _clean(value, 80)
                if athlete.get("displayName") or athlete.get("fullName"):
                    player_rows.append({
                        "name": _clean(athlete.get("displayName") or athlete.get("fullName"), 100),
                        "team": group_team,
                        "position": _clean((athlete.get("position") or {}).get("abbreviation")
                                           or (athlete.get("position") or {}).get("displayName"), 30),
                        "starter": athlete_row.get("starter"),
                        "statistics": stat_values,
                    })
    # Keep the profile small enough for prompts/logs while retaining role diversity.
    quarterbacks = [p for p in player_rows if p.get("position", "").upper() in ("QB", "QUARTERBACK")]
    skill_players = [p for p in player_rows if p.get("position", "").upper() in
                     ("RB", "HB", "FB", "WR", "TE", "RUNNING BACK", "WIDE RECEIVER", "TIGHT END")]
    defenders = [p for p in player_rows if p.get("position", "").upper() in
                 ("DE", "DT", "NT", "LB", "OLB", "ILB", "CB", "S", "FS", "SS", "EDGE")]
    injuries = raw.get("injuries") if isinstance(raw.get("injuries"), list) else []
    lineups_present = bool(raw.get("lineups") or raw.get("roster") or raw.get("rosters"))
    return {
        "sport_profile": "american_football",
        # NFL and NCAA are deliberately separate learning domains. Shared schema
        # does not mean shared baselines, calibration, or player/team histories.
        "league_domain": "nfl" if league_domain == "nfl" else "ncaa_football",
        "competition_level": "professional" if league_domain == "nfl" else "college",
        "learning_partition": "sports:nfl" if league_domain == "nfl" else "sports:ncaa_football",
        "event_id": event.get("provider_event_id"),
        "event": event.get("name") or event.get("short_name"),
        "observed_ts": observed_at,
        "source": "ESPN event summary",
        "teams": teams,
        "team_statistics": team_rows,
        "quarterbacks": quarterbacks[:8],
        "skill_players": skill_players[:16],
        "defensive_players": defenders[:12],
        "other_players": [p for p in player_rows if p not in quarterbacks and p not in skill_players and p not in defenders][:12],
        "injury_records": injuries[:30],
        "lineup_data_present": lineups_present,
        "lineups_confirmed": False,
        "data_quality": {
            "team_stats_available": bool(team_rows),
            "player_stats_available": bool(player_rows),
            "injury_data_available": bool(injuries),
            "lineup_data_available": lineups_present,
            "provider_fields_vary": True,
        },
        "missing_information": [
            label for label, present in (
                ("confirmed_inactives", bool(injuries)),
                ("confirmed_starting_lineups", lineups_present),
                ("team_statistics", bool(team_rows)),
                ("player_statistics", bool(player_rows)),
            ) if not present
        ],
        "interpretation_guardrail": "Descriptive source evidence only; do not infer availability from missing fields or treat this profile as a calibrated probability.",
    }


def _source_status(store, key, status):
    store.set_kv("sports_intel_source:" + key, status)

def _due(store, key, ttl, now):
    return now - float(store.get_kv("sports_intel_fetch:" + key, 0) or 0) >= ttl

def _record_items(store, items):
    inserted = 0
    for item in items:
        cur = store.x("""INSERT OR IGNORE INTO sports_intel_items
            (id, kind, category, source, title, url, summary, published_ts, observed_ts, confidence, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            item["id"], item["kind"], item["category"], item["source"], item.get("title") or item.get("name") or "",
            item.get("url", ""), item.get("summary", ""), item.get("published_ts"),
            item["observed_ts"], item.get("confidence", 0.5), json.dumps(item, separators=(",", ":"))))
        inserted += max(0, cur.rowcount)
    return inserted

def ensure_schema(store):
    store.x("""CREATE TABLE IF NOT EXISTS sports_intel_items (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, category TEXT, source TEXT,
        title TEXT, url TEXT, summary TEXT, published_ts INTEGER,
        observed_ts INTEGER NOT NULL, confidence REAL, payload_json TEXT NOT NULL)""")
    store.x("CREATE INDEX IF NOT EXISTS ix_sports_intel_recent ON sports_intel_items(kind, observed_ts)")
    store.x("CREATE INDEX IF NOT EXISTS ix_sports_intel_category ON sports_intel_items(category, observed_ts)")

def _summary_priority(item, now):
    event = item[3]
    state = event.get("state")
    start_ts = _iso_to_ts(event.get("start_time"))
    # Upcoming games nearest kickoff are most useful for starters/injuries;
    # live games next, then recently completed games for post-game learning.
    if state == "pre":
        return (0, abs(start_ts - now) if start_ts else 10**12)
    if state == "in":
        return (1, 0)
    return (2, 0)


def _select_summary_candidates(store, candidates, limit, now):
    """Fair-share scarce box-score requests across selected and other leagues."""
    limit = max(0, min(10, int(limit)))
    if limit == 0 or not candidates:
        return []
    grouped = {}
    for item in candidates:
        grouped.setdefault(item[0], []).append(item)
    for rows in grouped.values():
        rows.sort(key=lambda item: _summary_priority(item, now))

    # When manual mode selects known leagues, guarantee first-pass coverage for
    # each selected league that has an event. Other leagues still rotate through
    # the remaining budget rather than permanently monopolizing the first slots.
    selected_to_category = {
        "KXNFLGAME": "nfl",
        "KXNCAAFGAME": "college_football",
        "KXNBAGAME": "nba",
        "KXMLBGAME": "mlb",
        "KXNHLGAME": "nhl",
    }
    selected_series = {
        value.strip().upper() for value in os.environ.get("SPORTS_SELECTED", "").split(",")
        if value.strip()
    }
    preferred = []
    if os.environ.get("SPORTS_MODE", "all").strip().lower() == "manual":
        preferred = [selected_to_category[s] for s in selected_to_category
                     if s in selected_series and selected_to_category[s] in grouped]
    try:
        cursor = max(0, int(store.get_kv("sports_intel_summary_cursor", 0) or 0))
    except (TypeError, ValueError):
        cursor = 0
    if len(preferred) > limit:
        offset = cursor % len(preferred)
        preferred = preferred[offset:] + preferred[:offset]
    other = sorted(category for category in grouped if category not in preferred)
    if other:
        offset = cursor % len(other)
        other = other[offset:] + other[:offset]

    selected = []
    first_pass = preferred + other
    later_pass = other + preferred
    while len(selected) < limit:
        before = len(selected)
        order = first_pass if before == 0 else later_pass
        for category in order:
            if grouped.get(category) and len(selected) < limit:
                selected.append(grouped[category].pop(0))
        if len(selected) == before:
            break
    # Advance at least one category per collection, and proportionally more when
    # there are spare slots, so the non-selected sports get fair long-run coverage.
    store.set_kv("sports_intel_summary_cursor", cursor + max(1, limit - min(limit, len(preferred))))
    return selected


def collect(store, force=False, now=None, categories=None):
    """Collect recent free public sports news and scoreboards into the existing SQLite DB."""
    ensure_schema(store)
    now = int(now or _now())
    ttl = max(120, int(os.environ.get("SPORTS_INTEL_TTL_SECONDS", "1800")))
    selected_categories = set(SPORT_TERMS) if categories is None else (set(categories) & set(SPORT_TERMS))
    scoreboard_categories = set()
    for category in selected_categories:
        scoreboard_categories.update(SCOREBOARD_CATEGORIES.get(category, {category}))
    timeout_note = []
    summary = {"ok": True, "selected_categories": sorted(selected_categories),
               "news_seen": 0, "scores_seen": 0, "new_items": 0, "sources_ok": 0,
               "sources_failed": 0, "summaries_seen": 0, "team_stats_profiles": 0,
               "player_stats_profiles": 0, "injury_profiles": 0, "lineup_profiles": 0,
               "football_profiles": 0, "ts": now, "errors": []}
    # Google News RSS gives broad web discovery without an API key. Queries are cached
    # independently and kept deliberately modest for free-tier friendliness.
    for category, terms in SPORT_TERMS.items():
        if category not in selected_categories:
            continue
        key = "gnews:" + category
        if not force and not _due(store, key, ttl, now):
            continue
        query = urllib.parse.quote(f'({terms}) when:2d')
        url = "https://news.google.com/rss/search?q=" + query + "&hl=en-US&gl=US&ceid=US:en"
        try:
            response = _request(url)
            items = _parse_rss(response.content, "Google News RSS", category, now)
            # Ignore ancient feed results unless they are explicitly missing a date.
            items = [i for i in items if i.get("published_ts") is None or now - i["published_ts"] <= 7 * 86400]
            summary["news_seen"] += len(items)
            summary["new_items"] += _record_items(store, items)
            _source_status(store, key, {"ok": True, "ts": now, "count": len(items)})
            summary["sources_ok"] += 1
        except Exception as exc:
            summary["sources_failed"] += 1
            summary["errors"].append({"source": key, "error": _clean(exc, 240)})
            _source_status(store, key, {"ok": False, "ts": now, "error": _clean(exc, 240)})
            log.warning("sports intelligence source %s failed: %s", key, exc)
        store.set_kv("sports_intel_fetch:" + key, now)

    summary_candidates = []
    # Scoreboard JSON endpoints; one request per configured league per TTL window.
    high_frequency_scoreboards = {
        "nfl", "college_football", "nba", "wnba", "college_basketball",
        "mlb", "nhl", "soccer_mls", "soccer_epl", "soccer_uefa",
    }
    for category, (sport, league) in ESPN_SCOREBOARDS.items():
        if category not in scoreboard_categories:
            continue
        key = "espn:" + category
        source_ttl = ttl if category in high_frequency_scoreboards else max(ttl, 3600)
        if not force and not _due(store, key, source_ttl, now):
            continue
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        try:
            response = _request(url)
            events = _parse_espn(response.json(), "ESPN scoreboard", category, now)
            summary["scores_seen"] += len(events)
            normalized = []
            for event in events:
                event["id"] = hashlib.sha256((key + ":" + event["provider_event_id"] + ":" + str(now // max(ttl, 600))).encode()).hexdigest()
                event["confidence"] = 0.7 if event.get("state") == "in" else 0.55
                normalized.append(event)
                # Upcoming events are included too: pre-game summaries can expose
                # injuries, rosters, depth charts, and lineup announcements before the start.
                if event.get("state") in ("pre", "in", "post") and event.get("provider_event_id"):
                    summary_candidates.append((category, sport, league, event))
            summary["new_items"] += _record_items(store, normalized)
            _source_status(store, key, {"ok": True, "ts": now, "count": len(events)})
            summary["sources_ok"] += 1
        except Exception as exc:
            summary["sources_failed"] += 1
            summary["errors"].append({"source": key, "error": _clean(exc, 240)})
            _source_status(store, key, {"ok": False, "ts": now, "error": _clean(exc, 240)})
            log.warning("sports intelligence source %s failed: %s", key, exc)
        store.set_kv("sports_intel_fetch:" + key, now)

    # Fetch a bounded sample of detailed event summaries. Manual league
    # selections get first-pass coverage; the remaining slots rotate across all
    # other categories so one busy league cannot monopolize player-data requests.
    max_summaries = max(0, min(10, int(os.environ.get("SPORTS_INTEL_MAX_BOX_SCORES", "6"))))
    selected_summaries = _select_summary_candidates(store, summary_candidates, max_summaries, now)
    for category, sport, league, event in selected_summaries:
        provider_id = event["provider_event_id"]
        key = f"summary:{category}:{provider_id}"
        source_ttl = ttl if category in high_frequency_scoreboards else max(ttl, 3600)
        if not force and not _due(store, key, source_ttl, now):
            continue
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary"
        try:
            response = _request(url + "?event=" + urllib.parse.quote(provider_id))
            raw = response.json()
            box = raw.get("boxscore") or {}
            payload = {
                "id": hashlib.sha256(f"{key}:{now // max(source_ttl, 600)}".encode()).hexdigest(),
                "kind": "stats", "category": category, "source": "ESPN event summary",
                "title": event.get("name") or event.get("short_name") or provider_id,
                "observed_ts": now, "published_ts": None, "confidence": 0.65,
                "provider_event_id": provider_id, "state": event.get("state"),
                "scoreboard": event.get("competitors", []),
                "team_stats": box.get("teams", [])[:10],
                "player_stats": box.get("players", [])[:50],
                "leaders": raw.get("leaders", [])[:20],
                # Provider fields vary by sport and event state. Preserve useful
                # matchup evidence when present; absence is unknown, not "no injuries".
                "rosters": raw.get("rosters", [])[:20] if isinstance(raw.get("rosters"), list) else raw.get("rosters"),
                "lineups": raw.get("lineups", raw.get("roster", [])),
                "injuries": raw.get("injuries", [])[:30] if isinstance(raw.get("injuries"), list) else raw.get("injuries"),
                "depth_charts": raw.get("depthCharts", raw.get("depthcharts", [])),
                "plays": raw.get("plays", [])[-50:],
                "scoring_plays": raw.get("scoringPlays", [])[-30:],
                "win_probability": raw.get("winprobability", [])[-20:],
                "game_info": raw.get("gameInfo", {}),
                "season_series": raw.get("seasonseries", raw.get("headToHead", [])),
                "source_note": "ESPN public event summary; field availability varies by sport; verify lineup/injury freshness before use",
                "verified": False,
            }
            if category in ("nfl", "college_football"):
                payload["football_matchup_profile"] = _football_matchup_profile(event, box, raw, now, category)
                summary["football_profiles"] += 1
            summary["summaries_seen"] += 1
            summary["team_stats_profiles"] += int(bool(payload["team_stats"]))
            summary["player_stats_profiles"] += int(bool(payload["player_stats"]))
            summary["injury_profiles"] += int(bool(payload["injuries"]))
            summary["lineup_profiles"] += int(bool(payload["lineups"] or payload["rosters"] or payload["depth_charts"]))
            summary["new_items"] += _record_items(store, [payload])
            _source_status(store, key, {"ok": True, "ts": now, "count": 1})
            summary["sources_ok"] += 1
        except Exception as exc:
            summary["sources_failed"] += 1
            summary["errors"].append({"source": key, "error": _clean(exc, 240)})
            _source_status(store, key, {"ok": False, "ts": now, "error": _clean(exc, 240)})
            log.warning("sports intelligence source %s failed: %s", key, exc)
        store.set_kv("sports_intel_fetch:" + key, now)

    # Keep a compact health snapshot for the existing reports/diagnostics; no UI edits.
    summary["ok"] = summary["sources_ok"] > 0 or summary["sources_failed"] == 0
    store.set_kv("sports_intel_health", summary)
    store.set_kv("sports_intel_last_run", now)
    return summary

def collect_event_news(store, events, max_events=12, now=None):
    """Fetch event-targeted public news for a small rotating budget of upcoming games.

    Uses one cached RSS query per event, with a strict request budget to avoid
    hammering feeds. News remains unverified and is only an input to later review.
    """
    ensure_schema(store)
    now = int(now or _now())
    ttl = max(300, int(os.environ.get("SPORTS_INTEL_EVENT_TTL_SECONDS", "900")))
    queried = 0
    new_items = 0
    failures = 0
    candidates = []
    budget = max(0, min(50, int(max_events)))
    if budget == 0:
        return {"queried": 0, "new_items": 0, "failures": 0}
    def start_order(event):
        stamp = _iso_to_ts(event.get("start_time") or event.get("commence_time")
                           or event.get("strike_date") or event.get("date"))
        return stamp if stamp and stamp >= now else now + 10 * 86400
    for event in sorted(events, key=start_order):
        ticker = _clean(event.get("event_ticker") or event.get("ticker"), 180)
        if not ticker:
            continue
        key = "eventnews:" + ticker
        if not _due(store, key, ttl, now):
            continue
        market_rows = event.get("markets") or []
        teams = []
        for market in market_rows:
            name = market.get("yes_sub_title") or market.get("subtitle") or market.get("title")
            if name and name not in teams:
                teams.append(_clean(name, 100))
        event_name = _clean(event.get("title") or event.get("sub_title") or " ".join(teams), 180)
        if not teams and not event_name:
            continue
        # Prioritize a small set of useful, event-specific developments.
        names = teams[:4] if teams else [event_name]
        query_text = " ".join(f'"{name}"' for name in names)
        query_text += " (injury OR lineup OR roster OR suspension OR starter OR preview OR odds)"
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query_text) + "&hl=en-US&gl=US&ceid=US:en"
        candidates.append((key, ticker, url))
        if len(candidates) >= budget:
            break
    for key, ticker, url in candidates:
        queried += 1
        try:
            response = _request(url)
            items = _parse_rss(response.content, "Google News RSS event search", "event_news", now)
            for item in items:
                item["id"] = hashlib.sha256((item["id"] + ":" + ticker).encode()).hexdigest()
                item["event_ticker"] = ticker
                item["confidence"] = 0.4 if item.get("published_ts") else 0.2
            new_items += _record_items(store, items)
            _source_status(store, key, {"ok": True, "ts": now, "count": len(items)})
        except Exception as exc:
            failures += 1
            _source_status(store, key, {"ok": False, "ts": now, "error": _clean(exc, 240)})
            log.warning("event news source %s failed: %s", key, exc)
        store.set_kv("sports_intel_fetch:" + key, now)
    return {"queried": queried, "new_items": new_items, "failures": failures}

def recent(store, limit=100, category=None, kind=None):
    """Return newest stored evidence for briefing/model integration and diagnosis."""
    ensure_schema(store)
    where, args = [], []
    if category:
        where.append("category=?"); args.append(category)
    if kind:
        where.append("kind=?"); args.append(kind)
    sql = "SELECT payload_json FROM sports_intel_items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY observed_ts DESC LIMIT ?"
    args.append(max(1, min(1000, int(limit))))
    return [json.loads(row["payload_json"]) for row in store.q(sql, tuple(args))]

def health(store):
    return store.get_kv("sports_intel_health", {"ok": False, "errors": ["not run yet"]})

if __name__ == "__main__":
    from .config import Settings
    from .storage import Store
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    result = collect(Store(settings.db_path), force="--force" in os.sys.argv)
    print(json.dumps(result, indent=2))
