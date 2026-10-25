# Third-party MCP and research integration audit

Scope: existing Kalshi sports and crypto bot. This is an **integration boundary**, not a new trading service. No UI, trade sizing, or live mode changes.

## Reviewed ideas and disposition

| Source | Useful idea | Existing implementation / decision |
| --- | --- | --- |
| 9crusher/mcp-server-kalshi | Contract rules and settlement PDF evidence | Reuse `bot/market_rules.py` and `bot/contract_pdf.py`. New `bot/mcp_readonly.py` offers an allowlisted interface over the existing Kalshi client. Do not run a second network client. |
| BrainDAO/mcp-kalshi | Market, orderbook and portfolio queries | Reuse `bot/kalshi_client.py` market/series/orderbook, existing portfolio and ledger. No external MCP order, cancel, wallet or authentication tools. |
| OctagonAI/kalshi-trading-bot-cli | Risk gates, probability-versus-price review | Existing `bot/risk.py`, `bot/fees.py`, `bot/guard.py` and engine stay authoritative. Benchmark proposed sizing changes on time-split data before adoption. |
| rodlaf/KalshiMarketMaker | Inventory-aware quoting | Not imported. A market-maker strategy has different exposure, liquidity and cancellation assumptions; do not turn on by copying its formulas. |
| NWS / GFS | Weather forecasts | Deferred: current sports/crypto focus; requires specific weather market mapping, issue-time provenance, settlement-source matching, and backtests. |
| FRED | Official economic series | Deferred as a dedicated market feed; existing economic calendar remains. Add only for supported macro contracts with vintage/as-of timestamps. |
| FIX 4.4 | Execution transport | Not enabled. Needs confirmed Kalshi/account support, authenticated session/reconciliation tests, and risk-path parity. |
| OmniRoute / LiteLLM / OpenRouter | AI gateway resilience | Already routed through `bot/llm.py`; no parallel LLM agent stack or separate budget. |

## Safe data path

Existing `KalshiClient` → bounded read-only `research_tool` → existing rule summary / research → existing engine and risk controls. No new writes, collectors, database tables, dependencies, secrets, background tasks, or execution routes.

The adapter is **available to callers**, not automatically exposed as a network MCP server. Do not advertise it as installed external MCP connectivity. Any future tool broker must enforce the same allowlist before dispatch, authenticate its callers, and keep write operations inaccessible.

## Acceptance and next validation

Run `python -m unittest tests.test_mcp_readonly tests.test_llm_routing` and the repository's full CI. Verify no untrusted MCP payload can change settlement rules, odds, prices, risk thresholds, or live-mode approval. Review source licenses and pinned versions before vendoring third-party code; this change vendors none.
