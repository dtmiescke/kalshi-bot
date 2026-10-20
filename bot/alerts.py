"""Free phone alerts via ntfy.sh (no account needed).

1. Install the free "ntfy" app on your phone.
2. Subscribe to a long random topic name only you know, e.g. kalshi-bot-x7f3q9.
3. Add that name as the GitHub secret NTFY_TOPIC.
You'll get a push for every bet, sale, arbitrage, pause and model promotion.
"""
import os

import requests


def send(title, message, priority="default", tags=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8")[:3500], timeout=8,
                      headers={"Title": title[:200], "Priority": priority,
                               **({"Tags": ",".join(tags)} if tags else {})})
        return True
    except requests.RequestException:
        return False
