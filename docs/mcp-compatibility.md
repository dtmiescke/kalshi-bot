# Third-party MCP compatibility and integration boundary

This is an additive review of candidate functionality for the **existing** Kalshi Intelligence Bot. It does not deploy an MCP server, install an order-writing agent, enable live trading, or replace the UI. Candidate source code must be reviewed for license and security before copying.

## Source-to-existing-system map

| Source / idea | Useful capability | Existing integration / next action |
| --- | --- | --- |
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Market discovery, exact settlement terms, PDF rules, exchange status | **Reuse** the bot's existing market/series client and `bot/contract_pdf.py`; no second API client. This PR adds tests for trust boundaries, URL changes, cache and download limits. Validate settlement provenance before scoring new market types. |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Read-only order book, fills, position visibility | Compare output shapes with the existing client and persistence ledger. Do **not** expose wallet/order mutation methods to an LLM or run a second execution route. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Position sizing, risk evaluation, workflow ideas | Keep existing fractional-Kelly, fee, loss, correlation and bankroll gates authoritative. Evaluate alternatives offline on time-split historical outcomes before any opt-in switch. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory-aware quoting / spread modeling | Research-only until inventory, fees, fills, latency, adverse selection and cancellations are simulated with actual Kalshi book data. Never introduce an independent maker bot. |
| NWS / GFS | Weather forecast and observation evidence | Defer until a weather series is intentionally supported, settlement station/source is mapped, and publication timestamps prevent look-ahead. |
| FRED | Economic-series vintage and release data | Defer until supported macro contracts have explicit release/settlement definitions and vintage-safe replay. |
| FIX 4.4 | Potential institutional order transport | No adapter without official support/eligibility and end-to-end reconciliation. REST execution stays authoritative. |
| OmniRoute / LiteLLM / OpenRouter | LLM inference and fallback | Already routed through `bot/llm.py`; reuse shared budgets and existing research calls. Do not create another AI agent stack. |

## Non-negotiable contracts

1. One authoritative `KalshiClient`/execution path; third-party MCP output is **untrusted, read-only research**.
2. Normalize prices and YES/NO direction **only once** in the existing market/execution layer; do not copy a second translation layer without unit tests against the exchange schema.
3. Contract rules must come from verified Kalshi metadata/document URLs; changed URLs invalidate cached terms. Missing or failed documents remain unknown, never guessed.
4. Preserve source URL, observed timestamp, market/series ticker and stale/missing status for every optional evidence source; do not turn unavailable data into confidence.
5. No duplicate raw observations, new ledger, or independent positions/orders table; reuse current storage and janitor/watchdog.
6. Keep paper mode as the default. Live order activation requires existing approval gates and regression evidence.
7. Existing sports and crypto remain the scope; no weather/macro series or UI changes are silently enabled.

## Follow-up acceptance gates

Before activating any new read-only data adapter: compare its data with the existing client on matching market IDs; test rate limits, freshness, schema changes, duplicate handling, settlement-source consistency, missing data, and API outages. Add deterministic offline fixtures and run the full CI suite. The tests in `tests/test_mcp_readonly_contract_guards.py` cover the existing contract PDF trust boundary; they do **not** certify external MCP services or live-money readiness.
