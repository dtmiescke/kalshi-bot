# Third-party MCP / strategy integration audit (2026-10-25)

This project is **not** replacing the existing engine, UI, storage, LLM routing, or guarded order execution. These are engineering patterns reviewed from external projects, not audited or approved third-party dependencies.

| Source | Useful idea | Decision |
| --- | --- | --- |
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Consolidated contract terms, official PDF evidence, market depth and trades | **Adapt read-only** using existing `KalshiClient` and `contract_pdf`. No second REST client or order writer. |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Market discovery, portfolio visibility, normalized market data | **Reuse existing** market discovery, order books and portfolio reconciliation. Do not launch its authenticated MCP trading server. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Edge analysis, risk gates, half-Kelly, correlation and concentration checks | **Research only** until side-by-side walk-forward validation proves improvement net of fees. Do not change current sizing. Optional external research API may cost money. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Spread/depth screening, inventory caps, order cleanup | **Research only**; do not run an independent quoting worker, liquidation command or order cancellation loop. |
| NWS/GFS and FRED | Official weather/economic evidence | **Defer** until weather/macro contracts are explicitly enabled and settlement mapping is verified. |
| FIX 4.4 | Low-latency institutional order routing | **Defer** pending verified Kalshi/account compatibility, entitlement, recovery and reconciliation. |

## Implemented, additive capability

`bot/mcp_research.py` provides small **read-only research functions**: `market_rules_snapshot`, `orderbook_snapshot`, and `recent_trades_snapshot`. These call the existing Kalshi client (thus sharing pacing, retry, cache and price normalization). Contract PDF extraction uses the existing trusted-host, byte/page limits and cache, and is opt-in. No new package, collector, scheduled task, credentials or order path is introduced. They can be invoked by future research/agent tooling, but **this PR does not claim an MCP server is installed or that these functions are automatically called in each scan**.

## Required gates before wider activation

1. Verify no duplicate network polling or raw-data persistence in a normal scheduled cycle.
2. Verify real exchange settlement definitions against current official rules; missing data must remain unknown.
3. Benchmark extra requests against existing scheduled-run budget and Kalshi rate limits.
4. Compare risk and market-making ideas in time-ordered out-of-sample paper/shadow evaluation with fees, slippage, fills and correlated bets.
5. Run CI and repeated paper cycles; no live-trading activation without explicit approval.

Untrusted MCP/research text cannot instruct the bot to place an order or modify source code. The single existing risk/execution path remains authoritative.
