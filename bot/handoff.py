"""Run for hours without a gap, and hand over only when it is safe.

The bot streams in one long run instead of many short ones. Restarting costs a few
minutes in which nothing is watched, so a restart (a) happens as rarely as the time
limit allows and (b) always lands a few minutes into a 15-minute window, after the
read from 20 s before the start and the bet at 90 s are done, never across a boundary.
A deploy or a manual run waiting in line also ends the stream at the next such point,
so changes are not held up for hours.
"""
import logging
import os

import requests

log = logging.getLogger("bot")
WINDOW = 900
API = "https://api.github.com"


def stop_point(now, target_s, phase=240.0, min_s=240.0):
    """The last moment of a window's `phase` second at or before now + target_s,
    pushed to the next window if that would be under min_s away."""
    end = ((now + target_s - phase) // WINDOW) * WINDOW + phase
    while end - now < min_s:
        end += WINDOW
    return end


def next_stop(now, phase=240.0, min_ahead=45.0):
    """The next `phase` second of a window that is at least min_ahead seconds away."""
    end = (now // WINDOW) * WINDOW + phase
    while end - now < min_ahead:
        end += WINDOW
    return end


def restart_reason(start_sha, repo, token, get=requests.get, timeout=10):
    """Why the stream should end soon, or None. Never raises: no answer means keep going.

    - the bot was paused from the app (the workflow was disabled)
    - main moved on from the code this run started with (a deploy)
    - another run is waiting for the bot's lock, other than the 12-minute cron fallback
      (a push to the workflow, a manual run, the app's run button, clearing a pause)"""
    if not token or not repo:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        if start_sha:
            r = get(f"{API}/repos/{repo}/git/ref/heads/main", headers=headers, timeout=timeout)
            if r.status_code == 200:
                head = (r.json().get("object") or {}).get("sha")
                if head and head != start_sha:
                    return f"main moved to {head[:7]}"
        r = get(f"{API}/repos/{repo}/actions/workflows/bot.yml", headers=headers, timeout=timeout)
        if r.status_code == 200 and r.json().get("state") not in (None, "active"):
            return "the bot was paused"       # the app's pause button disables the workflow
        # "pending" = held back by a concurrency group; only these two workflows share the bot's lock
        r = get(f"{API}/repos/{repo}/actions/runs", params={"status": "pending", "per_page": 20},
                headers=headers, timeout=timeout)
        if r.status_code == 200:
            for run in r.json().get("workflow_runs", []):
                path = str(run.get("path", ""))
                if path.endswith("bot.yml") and run.get("event") == "schedule":
                    continue
                if path.endswith(("bot.yml", "clear-pause.yml")):
                    return f"{run.get('name', 'a run')} ({run.get('event')}) is waiting"
    except Exception as exc:       # network, JSON, rate limit: keep streaming
        log.debug("restart check failed: %s", exc)
    return None
