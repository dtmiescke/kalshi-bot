"""`python -m bot.main diagnose` — evidence, not guesses, when Kalshi refuses us.

Makes a handful of spaced-out requests (never a flood): unsigned first, then
signed with your data key if you have one, and records status codes,
Retry-After and any rate-limit headers. Output goes to the screen and to
DIAGNOSE.txt, ready to paste into a Kalshi support ticket if access stays
blocked. It does not try to evade a block in any way.
"""
import json
import platform
import time

import requests

from .kalshi_client import KalshiClient, request_log

PROBES = ["/exchange/status", "/series/KXBTC15M",
          "/events?series_ticker=KXBTC15M&status=open&limit=5"]


def _probe(client, path):
    p, _, q = path.partition("?")
    params = dict(x.split("=") for x in q.split("&")) if q else None
    try:
        d = client._req("GET", p, params=params, retries=1)
        return "OK " + str(list(d)[:4])
    except requests.HTTPError as e:
        return f"HTTP {e.response.status_code}: {e.response.text[:160]!r}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def run(settings):
    lines = ["=== KALSHI CONNECTION DIAGNOSIS ===",
             f"time (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
             f"host: {settings.market_data_url()}",
             f"runner: {platform.platform()}"]
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text
        lines.append(f"public IP of this machine: {ip}")
    except Exception:
        lines.append("public IP: unknown")
    lines.append("\n-- unsigned requests --")
    anon = KalshiClient(settings.market_data_url())
    for p in PROBES:
        lines.append(f"{p}: {_probe(anon, p)}")
        time.sleep(2)
    kid, pem = settings.data_key()
    if kid:
        lines.append("\n-- signed with your data key --")
        signed = KalshiClient(settings.market_data_url(), kid, pem)
        for p in PROBES:
            lines.append(f"{p}: {_probe(signed, p)}")
            time.sleep(2)
        try:
            signed._req("GET", "/portfolio/balance", retries=1)
            lines.append("key check (/portfolio/balance): accepted")
        except Exception as e:
            lines.append(f"key check (/portfolio/balance): {e}")
    else:
        lines.append("\n(no KALSHI_DATA_KEY_ID set: signed comparison skipped)")
    lines.append("\n-- raw response log --")
    lines += [json.dumps(r) for r in request_log()]
    lines.append("\nIf every request returns 429 even signed and spaced out, send this whole"
                 " report to Kalshi support and ask whether this IP or key is limited.")
    return "\n".join(lines)
