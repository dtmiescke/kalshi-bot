# Open-source Kalshi/MCP integration review

This review adapts useful *design ideas* to the existing Python/SQLite bot. It does
not vendor third-party runtimes, install a second exchange client, execute downloaded
code, or copy large blocks of source. Re-check upstream licenses and API behavior before
any future reuse of implementation code.

## Results

| Reference | Useful mechanism | Decision in this repository |
|---|---|---|
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Read market/event metadata, order books, and the actual contract settlement terms; normalize YES/NO concepts | Reuse the existing `KalshiClient` and `bot/market_rules.py`. Candidate snapshots now preserve bounded rule fields, and the shortlisted sports candidate gets one direct market-detail lookup if nested event data omitted the rules. Missing rules fail closed. No MCP process is added; the bot uses a bounded official Kalshi contract-PDF fallback through `pypdf` when existing metadata provides an allowlisted URL. |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Market/order-book/portfolio read tools | The existing client already owns public market data and the signed portfolio/order API. Do not add a second authenticated client: it would create duplicate rate-limit state, stale cache behavior, credentials, and potentially conflicting order-state tracking. Trading tools from this MCP remain excluded. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Independent probability estimate, edge calculation, liquidity checks, risk gates, auditability | Existing model/fees/risk/guard/counsel flow already covers these responsibilities. Do not copy its Half-Kelly defaults or external research dependency. Any new sizing method must beat the existing sizing in fee-aware, time-ordered out-of-sample tests before it can affect sizing. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Binary-market eligibility, liquidity selection, inventory limits, cancellation/worker lifecycle | Useful operational concepts, but its two-sided resting market-making loop is not the current sports/crypto strategy. Do not add competing resting-order workers or autonomous quote placement. |
| NWS/GFS weather feeds | Weather forecast evidence | Not wired into sports or crypto probability. Add only alongside an explicitly supported weather-contract category and settlement-source validation. |
| FRED macro series | Official macroeconomic time series | Not injected into sports/crypto. Add only when the bot supports macro contracts with a timestamp-aware release calendar and matching settlement definitions. |
| FIX 4.4 | Low-latency execution protocol | Not activated. The current guarded REST path remains authoritative until official account eligibility, supported venue connectivity, idempotent order reconciliation, and failure/latency tests exist. |
| OmniRoute / LiteLLM / OpenRouter | OpenAI-compatible gateway routing with shared budgets | Already integrated as OmniRoute → optional LiteLLM proxy → OpenRouter. OpenRouter-specific web search remains on OpenRouter. |

## Changes made in this review

1. `Engine.snapshot()` now keeps only the six bounded rule fields needed by counsel
   (`rules_primary`, `rules_secondary`, `early_close_condition`,
   `settlement_value`, `settlement_source`, `custom_strike`). The persistent
   market-snapshot schema and dashboard are unchanged.
2. If nested event data omitted all contract terms, the engine performs one direct
   market-detail lookup for the single best shortlisted candidate. It copies rule fields
   only, never overwrites the fresh price snapshot, and treats lookup failure as unknown.
3. Sports counsel now waits if rule terms are unavailable. If terms contain early-close
   conditions that require review, it also waits when counsel is disabled or its AI budget
   is exhausted. AI counsel remains a veto/review layer: it cannot create a candidate,
   change size, or bypass the numeric risk gate.
4. Added offline tests for rule-field preservation, bounded context, missing-rule behavior,
   unavailable AI counsel, series restrictions/document URL allowlisting, common MCP field
   aliases, safe official-PDF extraction/cache behavior, and shared compact rules auditing in
   both sports counsel and crypto settlement metadata. Prohibition-only payloads and
   PDF text without a settlement source cannot satisfy the required settlement-rule check.
   No live order was sent.
5. Added an optional official contract-PDF fallback: HTTPS-only allowlisted hosts, no
   redirects, an 8 MiB download cap, 30-page / 6,000-character extraction cap, seven-day
   cache, and at most 3,000 characters passed to counsel. Extraction errors remain unknown.

## MCP protocol compatibility hardening

The rule normalizer accepts native Kalshi objects, known read-only MCP wrappers, MCP structuredContent, and JSON encoded in MCP text content blocks. Non-JSON text is retained only as bounded PDF-like evidence and cannot satisfy the settlement-source gate; explicit MCP tool errors and non-text/empty payloads fail closed. This is a small compatibility adapter, not a running MCP client/server, and adds neither credentials nor a second order path.

## Deliberately not added

- No separate MCP server with order-write access.
- No new API credentials, MCP runtime, external exchange client, or always-on service; `pypdf` is used only for bounded PDF text extraction.
- No NWS/GFS/FRED feeds in unrelated market categories.
- No market-making strategy, Half-Kelly replacement, or FIX transport.
- No automatic source-code modification based on web research.

## Acceptance criteria for any later integration

- Uses the existing data client, call budgets, event/ticker identity, storage, and final
  guarded execution path.
- Has explicit freshness and settlement-source rules; missing or contradictory data fails
  closed instead of being guessed by an LLM.
- Has deterministic offline tests, no duplicate observations, and no schema/UI change unless
  separately requested.
- Demonstrates improvement on time-split, unseen data after fees and realistic fills.
- Keeps live trading disabled unless the existing operational approval gates are satisfied.
