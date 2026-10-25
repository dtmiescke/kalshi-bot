# External MCP and trading-tool compatibility review (2026-10-25)

This is an integration **review**, not approval to run external trading agents. The existing Python KalshiClient, rate limiting, market research, single execution authority, paper defaults, risk checks, data retention and UI remain authoritative.

| Reference | Useful ideas | Integration decision |
| --- | --- | --- |
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Exact primary/secondary contract rules, settlement sources, contract PDF references, market discovery | Adapt read-only contract research to existing client. Never expose create/cancel/amend order tools or launch a parallel MCP server. PDF links are references only; do not fetch untrusted URLs. |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Market depth and portfolio observability | Existing KalshiClient already has market, book, trades, balance and positions. Avoid redundant collectors and an additional trading path. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Research provenance, calibration, correlation and liquidity diagnostics | Evaluate as offline candidate features against existing risk/council and historical decisions. Do not copy its separate Bun runtime, proprietary research dependencies, half-Kelly sizing or five-gate execution engine. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Resting-order cleanup, inventory limits, graceful shutdown | Compare invariants in existing maker-order lifecycle. Do not add continuous quoting or Avellaneda–Stoikov workers to a scheduled directional bot. |
| NWS/GFS and FRED | Weather forecasts and official macro series | Deferred: currently configured sports and crypto do not need new weather/macro collectors. Require market-specific settlement definitions, timestamps, revision policies and licensing checks before adding. |
| FIX 4.4 | Potential lower-latency exchange access | Deferred pending confirmed Kalshi/account support and a reconciled single execution path. No assumptions about protocol availability. |
| OmniRoute, LiteLLM, OpenRouter | Gateway redundancy | Already integrated; keep the shared call budget and OpenRouter-specific search behavior. |

## Contract-research adapter

`bot.contract_research.contract_research(existing_client, ticker)` reads official market and series metadata through the **existing** `KalshiClient`. It reports missing rules and settlement sources explicitly, rejects mismatched tickers, and only returns safe HTTPS document references. It does not download PDFs, place orders, infer probabilities or activate additional sports.

The adapter is intentionally **research-only and opt-in**, not automatically wired into every scheduled scan: per-market series calls can exhaust API budgets. A future consumer must cache by series, bound calls, carry freshness timestamps and test coverage before introducing the data to decision-making. Existing settlement checks remain unchanged.

## Acceptance criteria for later stages

- No duplicated data or new unguarded order path.
- No execution changes without replay, fee/slippage and settlement-source tests.
- Preserve paper/live gating, risk caps, existing dashboard, sport selection and janitor/watchdog tasks.
- Unknown, stale or conflicting contract rules must be marked unknown and must never be interpreted as a positive trading signal.
- External project code must receive dependency/license/security review before any direct code copying.
