"""Silent-failure detector.

Catches the class of bug where something quietly returns nothing (the order-book
bug: 0 of 488 markets priced for days). Each check returns a list of alarms;
an empty list means healthy. Checks never raise: a broken check is itself an alarm.

Wired in by nerd.silent_failure_check at the end of every scheduled cycle: stats
come from what the cycle wrote to the database, the result is stored in the
Nerd's report (kv nerd_last_health["silent_failures"]), critical alarms alert
and pause only their area via guard.league_benched until a clean cycle.
"""
from dataclasses import dataclass, field
import time


@dataclass
class Alarm:
    check: str
    severity: str          # "warning" or "critical"
    message: str
    area: str = "all"      # which betting area to pause: crypto, sports, ladders, all
    data: dict = field(default_factory=dict)


def check_pricing(stats):
    """Share of markets that got a usable price. Zero is never normal."""
    out = []
    for area, s in (stats.get("pricing") or {}).items():
        seen, priced = s.get("seen", 0), s.get("priced", 0)
        if seen >= 20 and priced == 0:
            out.append(Alarm("pricing", "critical",
                             f"{area}: 0 of {seen} markets priced. Price reading is likely broken.",
                             area, {"seen": seen}))
        elif seen >= 20 and priced / seen < s.get("min_share", 0.05):
            out.append(Alarm("pricing", "warning",
                             f"{area}: only {priced} of {seen} markets priced.", area))
    return out


def check_sports_coverage(stats):
    """On a day with scheduled games, each league must produce markets."""
    out = []
    for league, s in (stats.get("sports") or {}).items():
        if s.get("games_scheduled", 0) > 0 and s.get("markets_found", 0) == 0:
            out.append(Alarm("sports_coverage", "critical",
                             f"{league}: {s['games_scheduled']} games today but 0 Kalshi markets found.",
                             "sports"))
    return out


def check_ungraded_bets(open_bets, now=None, grace_seconds=1800):
    """Any bet still open well past when its market should have settled.

    open_bets: list of dicts with 'ticker' and 'expected_settle_ts' (unix seconds).
    These are the bets to look up in Kalshi's historical archive.
    """
    now = now or time.time()
    stale = [b for b in open_bets
             if b.get("expected_settle_ts") and now - b["expected_settle_ts"] > grace_seconds]
    if not stale:
        return []
    return [Alarm("ungraded_bets", "warning",
                  f"{len(stale)} bet(s) still open 30+ min after settlement; checking archive.",
                  "all", {"tickers": [b["ticker"] for b in stale]})]


def check_fields(records, required, name):
    """Required fields must be present and non-empty; missing ones are counted, never zeroed."""
    missing = {f: 0 for f in required}
    for r in records:
        for f in required:
            if r.get(f) in (None, ""):
                missing[f] += 1
    bad = {f: c for f, c in missing.items() if c}
    if not bad or not records:
        return []
    worst = max(bad.values()) / len(records)
    sev = "critical" if worst >= 0.5 else "warning"
    return [Alarm("fields", sev, f"{name}: missing fields {bad} in {len(records)} records.",
                  "all", {"missing": bad})]


def check_signals(stats):
    """Signals and Council votes should not silently drop to nothing."""
    out = []
    s = stats.get("signals") or {}
    if s.get("candidates", 0) > 0 and s.get("signals_produced", 0) == 0:
        out.append(Alarm("signals", "critical", "Candidates found but no signals produced."))
    c = stats.get("council") or {}
    if c.get("sessions", 0) > 0 and c.get("votes", 0) == 0:
        out.append(Alarm("council", "warning", "Council met but cast no votes."))
    return out


CHECKS = (check_pricing, check_sports_coverage, check_signals)


def run_all(stats, open_bets=None, field_sets=None, now=None):
    alarms = []
    for chk in CHECKS:
        try:
            alarms += chk(stats)
        except Exception as e:  # a broken check is itself a problem worth seeing
            alarms.append(Alarm(chk.__name__, "warning", f"check failed: {e}"))
    try:
        alarms += check_ungraded_bets(open_bets or [], now=now)
    except Exception as e:
        alarms.append(Alarm("ungraded_bets", "warning", f"check failed: {e}"))
    for name, (records, required) in (field_sets or {}).items():
        try:
            alarms += check_fields(records, required, name)
        except Exception as e:
            alarms.append(Alarm("fields", "warning", f"{name}: check failed: {e}"))
    return alarms


def paused_areas(alarms):
    """Areas where betting must pause this run (critical alarms only)."""
    return sorted({a.area for a in alarms if a.severity == "critical"})
