"""AI client: OmniRoute and/or OpenRouter, several models, automatic fallback,
hard budget.

Endpoints, tried in this order:
1. OmniRoute (optional): a self-hosted free AI gateway. Set OMNIROUTE_URL to its
   OpenAI-compatible base (e.g. https://your-host/v1), OMNIROUTE_API_KEY, and
   OMNIROUTE_MODELS (model or combo names from your OmniRoute dashboard).
2. OpenRouter: OPENROUTER_API_KEY + OPENROUTER_MODELS (free ":free" models).
If OmniRoute is unreachable, every call falls through to OpenRouter.


Free models come and go on OpenRouter, so the list is configurable
(OPENROUTER_MODELS, comma-separated) and any model that errors is skipped.
Free accounts also have daily request limits, so every call is counted
against LLM_DAILY_LIMIT and LLM_MAX_CALLS_PER_RUN.
"""
import json
import os
import re
import threading
import time

import requests

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "qwen/qwen-2.5-72b-instruct:free",
]


def endpoints_from_env(openrouter_key=None):
    """[(name, chat_url, key)] in priority order."""
    eps = []
    base = os.environ.get("OMNIROUTE_URL", "").rstrip("/")
    if base:
        eps.append(("omniroute", base + "/chat/completions", os.environ.get("OMNIROUTE_API_KEY", "")))
    if openrouter_key:
        eps.append(("openrouter", URL, openrouter_key))
    return eps


def models_from_env():
    raw = os.environ.get("OPENROUTER_MODELS", "")
    return [m.strip() for m in raw.split(",") if m.strip()] or DEFAULT_MODELS


class LLM:
    def __init__(self, api_key, store, models=None, max_per_run=None, daily_limit=None,
                 post=None, endpoints=None):
        self.key = api_key
        self.endpoints = {n: (u, k) for n, u, k in (endpoints or endpoints_from_env(api_key))}
        if api_key and "openrouter" not in self.endpoints:
            self.endpoints["openrouter"] = (URL, api_key)
        self.store = store
        if models is None:
            om = [m.strip() for m in os.environ.get("OMNIROUTE_MODELS", "auto").split(",") if m.strip()]
            models = ([f"omniroute|{m}" for m in om] if "omniroute" in self.endpoints else []) + \
                models_from_env()
        # labels: "omniroute|<model>" or a plain OpenRouter model name
        self.models = models
        self.max_per_run = int(max_per_run or os.environ.get("LLM_MAX_CALLS_PER_RUN", 6))
        self.daily_limit = int(daily_limit or os.environ.get("LLM_DAILY_LIMIT", 45))
        self.calls_this_run = 0
        self._post = post or requests.post
        self.dead = set()       # models that failed this run
        self._lock = threading.Lock()

    def _day_key(self):
        return "llm_calls_" + time.strftime("%Y%m%d", time.gmtime())

    def budget_left(self):
        used = self.store.get_kv(self._day_key(), 0)
        return min(self.daily_limit - used, self.max_per_run - self.calls_this_run)

    def ask(self, prompt, model=None, temperature=0.1, max_tokens=400):
        """Return (text, model_used) or (None, None). Falls back across models."""
        if not self.endpoints or self.budget_left() <= 0:
            return None, None
        order = ([model] if model else []) + [m for m in self.models if m != model]
        for m in order:
            with self._lock:   # council calls run in parallel threads
                if m in self.dead or self.budget_left() <= 0:
                    continue
                self.calls_this_run += 1
                self.store.set_kv(self._day_key(), self.store.get_kv(self._day_key(), 0) + 1)
            ep, name = m.split("|", 1) if "|" in m else ("openrouter", m)
            if ep not in self.endpoints or ep in self.dead:
                continue
            url, key = self.endpoints[ep]
            try:
                r = self._post(url, timeout=60, headers={
                    "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": name, "temperature": temperature, "max_tokens": max_tokens,
                          "messages": [{"role": "user", "content": prompt}]})
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"], m
            except requests.ConnectionError:
                self.dead.add(ep)      # gateway down: skip ALL its models this run
            except Exception:
                self.dead.add(m)
        return None, None

    def ask_json(self, prompt, model=None):
        text, m = self.ask(prompt, model=model)
        if not text:
            return None, None
        hit = re.search(r"\{.*\}", text, re.S)
        try:
            return (json.loads(hit.group(0)) if hit else None), m
        except ValueError:
            return None, m

    def panel(self, prompt, n=2):
        """Ask up to n DIFFERENT models the same question: a panel of analysts.
        Disagreement between them is information, not noise."""
        out = []
        for m in self.models:
            if len(out) >= n or self.budget_left() <= 0:
                break
            if m in self.dead:
                continue
            data, used = self.ask_json(prompt, model=m)
            if data and used == m:
                out.append((m, data))
        return out
