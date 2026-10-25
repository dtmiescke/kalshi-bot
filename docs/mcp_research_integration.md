# External MCP / research integration review (2026-10-25)

This project **extends** the existing Kalshi bot. No second order engine, database,
websocket collector, dashboard, LLM gateway, or risk policy is introduced.

## What was adapted

- [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi):
  contract rules, series settlement metadata, explicit provenance, and read-only
  market snapshots. `bot.readonly_research.ReadOnlyKalshiResearch` delegates
  to the existing `KalshiClient`; no subprocess or MCP runtime is installed.
  Contract PDFs are **not** fetched or parsed automatically: a URL is evidence,
  not proof the PDF was read.
- [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi):
  market lookup, order-book and recent-trade research concepts only. Portfolio,
  balance, fill, cancel, and create-order tools are **not exposed** to research.
- [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli):
  considered correlation checks, independent probability research, and
  fractional-Kelly risk gates. Existing `bot/config.py` already has fractional
  Kelly, position caps and loss limits. No competing risk engine or Octagon
  account dependency was added; any additional factor requires retrospective
  comparison, calibration, and out-of-sample testing.
- OmniRoute / LiteLLM / OpenRouter: the existing `bot/llm.py` priority and
  budgets are preserved. This bridge does not give any LLM trade permissions.

## How to use it in existing research code

```python
from bot.readonly_research import ReadOnlyKalshiResearch

research = ReadOnlyKalshiResearch(existing_kalshi_client)
rules = research.get_market_rules("YOUR_MARKET_TICKER")
snapshot = research.get_market_snapshot("YOUR_MARKET_TICKER", depth=10)
```

This facade is opt-in for research call sites; **no live or scheduled strategy
automatically changes behavior** just by merging it. Callers must treat
`status=unavailable` or `status=incomplete` as unknown, check timestamps,
and verify contract PDFs and official settlement rules independently.

## Deliberately not installed

- A third-party MCP server with Kalshi order-write tools. Running two
  independent trading clients could bypass the current engine's caps,
  persistence, audit and circuit breakers.
- Octagon's proprietary edge estimates or its separate SQLite state.
- Avellaneda–Stoikov market making, FIX 4.4, and third-party execution
  algorithms: require eligibility, fees/fill assumptions, and safe reconciliation.
- NWS/GFS weather and FRED macro ingestion: add only alongside intentionally
  supported weather/macro markets and explicit freshness/settlement checks.
- PDF download/extraction and remote MCP process execution: these require
  bounded network and untrusted-document handling before activation.

## Review / verification

Run `python -m unittest tests.test_readonly_research` and the full repository
test suite. The new facade has no method that submits, cancels, or amends an
order, and uses the original KalshiClient for all network calls and rate pacing.
No new secret, dependency, workflow schedule, UI change, or live-trading
permission is required.
