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
    "esports": "esports roster change match result tournament",
}
RSS_CATEGORIES = tuple(SPORT_TERMS)

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
    return re.sub(r"\\s+", " ", str(value or "")).strip()[:limit]

def _request(url):
    response = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/json, */*"},
                            timeout=TIMEOUT)
    response.raise_for_status()
    if len(response.content) > MAX_BODY:
        raise ValueError("response exceeded size limit")
    return response

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
        event_row = {
            "id": _clean(event.get("id")), "provider_event_id": _clean(event.get("id")),
            "source": source, "category": category,
            "kind": "score", "name": _clean(event.get("name")),
            "short_name": _clean(event.get("shortName")),
            "start_time": event.get("date"), "observed_ts": observed_at,
            "state": _clean(status_type.get("state")), "status": _clean(status_type.get("description")),
            "detail": _clean(status_type.get("detail")), "clock": _clean(status.get("displayClock")),
            "period": status.get("period"), "competitors": competitors,
            "odds": (comp.get("odds") or event.get("odds") or [])[:10],
            "predictor": event.get("predictor") or comp.get("predictor"),
            "verified": False,
        }
        if event_row["id"] and competitors:
            events.append(event_row)
    return events

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

def collect(store, force=False, now=None):
    """Collect recent free public sports news and scoreboards into the existing SQLite DB."""
    ensure_schema(store)
    now = int(now or _now())
    ttl = max(120, int(os.environ.get("SPORTS_INTEL_TTL_SECONDS", "600")))
    timeout_note = []
    summary = {"ok": True, "news_seen": 0, "scores_seen": 0, "new_items": 0, "sources_ok": 0,
               "sources_failed": 0, "ts": now, "errors": []}
    # Google News RSS gives broad web discovery without an API key. Queries are cached
    # independently and kept deliberately modest for free-tier friendliness.
    for category, terms in SPORT_TERMS.items():
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
    for category, (sport, league) in ESPN_SCOREBOARDS.items():
        key = "espn:" + category
        if not force and not _due(store, key, ttl, now):
            continue
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        try:
            response = _request(url)
            events = _parse_espn(response.json(), "ESPN scoreboard", category, now)
            summary["scores_seen"] += len(events)
            normalized = []
            for event in events:
                event["id"] = hashlib.sha256((key + ":" + event["id"]).encode()).hexdigest()
                event["confidence"] = 0.7 if event.get("state") == "in" else 0.55
                normalized.append(event)
                if event.get("state") in ("in", "post") and event.get("provider_event_id"):
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

    # Fetch detailed live/final box scores for a small bounded sample. Scoreboard
    # payloads stay lightweight; detailed stats are cached per event and time window.
    max_summaries = max(0, min(10, int(os.environ.get("SPORTS_INTEL_MAX_BOX_SCORES", "3"))))
    summary_candidates.sort(key=lambda item: 0 if item[3].get("state") == "in" else 1)
    for category, sport, league, event in summary_candidates[:max_summaries]:
        provider_id = event["provider_event_id"]
        key = f"summary:{category}:{provider_id}"
        if not force and not _due(store, key, ttl, now):
            continue
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary"
        try:
            response = _request(url + "?event=" + urllib.parse.quote(provider_id))
            raw = response.json()
            box = raw.get("boxscore") or {}
            payload = {
                "id": hashlib.sha256(f"{key}:{now // max(ttl, 600)}".encode()).hexdigest(),
                "kind": "stats", "category": category, "source": "ESPN event summary",
                "title": event.get("name") or event.get("short_name") or provider_id,
                "observed_ts": now, "published_ts": None, "confidence": 0.65,
                "provider_event_id": provider_id, "state": event.get("state"),
                "scoreboard": event.get("competitors", []),
                "team_stats": box.get("teams", [])[:10],
                "player_stats": box.get("players", [])[:20],
                "leaders": raw.get("leaders", [])[:20],
                "scoring_plays": raw.get("scoringPlays", [])[:30],
                "win_probability": raw.get("winprobability", [])[-20:],
                "verified": False,
            }
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
    for event in events:
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
