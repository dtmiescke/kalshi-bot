# External MCP and trading-tool integration review

This is an implementation review for the existing bot, not a second bot or a replacement architecture. The integration rule is: **reuse the current data clients, market model, ledger, risk gates, and single guarded execution path**. External code is treated as untrusted input and is not installed or executed at runtime.

## Findings and disposition

| Source | Useful design | Decision in this repository |
|---|---|---|
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Market/event discovery, normalized order books, settlement-rule summaries, reading actual contract-terms PDFs, exchange status | Reuse the read-only design. Existing `KalshiClient` already owns discovery, market snapshots, order books, trades, request pacing, and caching; `bot/market_rules.py` summarizes supplied terms. Sports counsel now has a deterministic fail-closed gate: absent or ambiguous rules return `wait` even when LLM counsel is off or unavailable. PDF extraction is not yet wired because the current market payload path does not establish a trustworthy PDF URL and the repo does not include a PDF parser; do not fetch arbitrary URLs or silently invent terms. |
| [BrainDAO / IQAI mcp-kalshi](https://github.com/IQAIcom/mcp-kalshi) | Search, real-time prices, order book, positions, fills, settlements | Do not run as a second exchange client. Those read capabilities overlap existing code; portfolio/order-writing tools would create a second execution and reconciliation path. |
| [OctagonAI kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Explicit pre-trade risk gates, separate probability estimate, catalyst research, liquidity/correlation/concentration checks | Keep the pattern, not its bot or external research dependency. This repo already has independent probability, fee-aware edge, fractional Kelly, liquidity-aware fill planning, position/concentration caps, drawdown guards, and counsel. Any additional risk gate must be tested against the existing gate ordering and historical data before it can alter trades. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory-aware quoting, market selection, bounded exposure, cancellation/reconciliation lifecycle | Do not transplant the Avellaneda–Stoikov worker loop. It is a separate market-making strategy with resting orders, while this bot is primarily directional sports/crypto with a guarded order path. Use only as a future offline benchmark if market-making becomes an explicit strategy. |
| NWS / GFS | Weather observations and forecasts | Not wired into sports/crypto signals. Add only alongside explicitly supported weather contracts, official settlement-source mapping, timestamps, forecast-vintage handling, and out-of-sample evaluation. |
| FRED | Macro/economic time series | Not wired into sports/crypto signals. Add only for supported macro contracts with publication timestamps, revisions/vintage handling, and settlement mapping. Existing macro calendar context is not treated as a settlement source. |
| FIX 4.4 | Standardized order protocol | Not enabled. Requires official Kalshi account/API eligibility, documented protocol support, order-state reconciliation, and latency/failure testing. Existing REST client remains the only execution authority. |
| OmniRoute / LiteLLM / OpenRouter | Gateway routing and fallback | Already connected in the existing LLM client as OmniRoute → LiteLLM Proxy → OpenRouter. OpenRouter-specific web search stays on OpenRouter. All routes share existing budgets and concise-output policy. |

## Current safe flow

1. Existing discovery and feeds collect data once through current clients.
2. The current market/risk pipeline computes probabilities, fees, edge, size, liquidity, and caps.
3. Sports counsel receives a bounded summary of the exact settlement fields supplied by Kalshi.
4. If those terms are absent or the current parser flags them as requiring review, counsel returns `wait` before an AI response can approve the candidate.
5. Only the existing engine and `KalshiClient` can submit orders; the final freshness/risk checks, ledger, and reconciliation remain authoritative.
6. External web pages, MCP outputs, and LLM summaries are research evidence only. They cannot change limits, enable live mode, or write code.

## Important limitations

- This is a compatibility review, not a claim that third-party code was installed, that a separate MCP server is running, or that every external API was exercised against live accounts.
- PDF-based legal terms remain a follow-up. A safe implementation needs a URL sourced from the official market/series payload, strict host allowlisting for Kalshi-owned document hosts, response-size and timeout limits, bounded PDF parsing, provenance, cache expiry, and tests for malformed/empty PDFs.
- Weather/macro feeds and FIX should stay out until the corresponding markets and official settlement rules are explicitly supported.
- No new dependency, secret, external order route, UI replacement, or live-trading activation is introduced by this review.
