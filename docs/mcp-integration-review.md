# External MCP and strategy review

This review borrows narrowly useful, inspectable ideas from public repositories; it does
not copy whole bots or run external MCP servers inside the scheduled trading process.

## Adopted in this PR

- **9crusher/mcp-server-kalshi — legal contract context:** the useful idea is to combine
  market-level rules with series-level settlement sources, prohibitions, and contract
  document links. The existing `KalshiClient` now exposes a read-only `series(ticker)`
  lookup using its existing request pacing/cache. The existing sports counsel enriches
  its rule review from that response. Missing metadata remains missing; series restrictions
  and early-close language cause manual review instead of an AI override.
- **One data/execution authority:** market, series, price, and order access continue through
  the bot's own `KalshiClient`. No separate MCP order endpoint, second credential store,
  or competing trade ledger is added. The existing final risk check and execution lock remain
  the only path for any order.
- **Research efficiency:** metadata uses the client's existing series cache/rate pacing.
  Contract document URLs are surfaced only as references; this PR does not download or parse
  PDFs, claim the PDF was verified, or trust arbitrary URLs.
- **LLM routing:** OmniRoute → optional LiteLLM → OpenRouter remains unchanged. Provider-
  specific OpenRouter web-search requests stay on OpenRouter. Concise-output controls may
  reduce tokens but cannot override schemas, evidence, uncertainty, or risk limits.

## Reviewed, but intentionally not copied or activated

| Project / idea | Decision | Reason |
|---|---|---|
| BrainDAO / IQAI Kalshi MCP | Do not connect order-writing or portfolio actions | Duplicates the bot's authenticated order path and creates reconciliation/risk bypass risk. Its read-only concepts are already covered by the existing client. |
| OctagonAI Kalshi CLI / five-gate risk checks | Use as a review checklist, not a second engine | This project already has edge-after-fees, fractional-Kelly sizing, freshness, liquidity, correlation/concentration, drawdown, counsel, and final execution gates. Another risk engine could disagree or double-count constraints. |
| Half-Kelly | Do not replace current sizing | The bot's existing configurable fractional Kelly is already more conservative by default; any change needs out-of-sample evidence and a separate approved model change. |
| Avellaneda–Stoikov market maker | Do not run as a trader | It is a resting-quote inventory strategy, not a drop-in improvement for this sports/crypto directional strategy. It would add persistent orders and new cancellation/reconciliation hazards. |
| FIX 4.4 | Do not enable | No demonstrated Kalshi FIX entitlement or supported integration is established here. The existing REST execution/reconciliation path stays authoritative. |
| NWS/GFS weather feeds | Defer | No weather-market strategy is explicitly enabled. A feed is useful only with a mapped supported series, official settlement source, freshness checks, and a historical evaluation. |
| FRED macro feeds | Defer | The current supported focus is sports and short-horizon crypto. Adding macro ingestion without an enabled macro-market strategy would add maintenance and stale-data risk without a validated use. |

## Safety invariants

1. Market and series lookups are read-only.
2. No API keys or private keys are added to code or logs.
3. Missing or conflicting settlement terms never become fabricated certainty.
4. The LLM can recommend wait/reject/approve only within existing counsel rules; it cannot
   create candidates, change stake size, bypass final risk checks, or place orders itself.
5. No UI, database schema, strategy weights, trade settings, or live-trading approvals change.
6. New metadata behavior must pass unit tests and repository CI before merge.

## Upstream references

- https://github.com/9crusher/mcp-server-kalshi
- https://github.com/BrainDAO/mcp-kalshi
- https://github.com/OctagonAI/kalshi-trading-bot-cli
- https://github.com/rodlaf/KalshiMarketMaker
