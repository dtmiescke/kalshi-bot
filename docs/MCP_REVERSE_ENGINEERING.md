# MCP and trading-tool reverse-engineering review

Reviewed 2026-10-25. This is an integration decision record for the existing bot, not a second bot or a request to install third-party MCP servers at runtime.

## Decision: reuse the existing owners

| Source | Useful idea | Fit with this bot | Decision |
|---|---|---|---|
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Market/event discovery, order-book/trade reads, exact contract terms and rules-PDF text | The bot already has one rate-limited KalshiClient, market_rules.summarize_rules, and sports-counsel checks for missing or ambiguous terms. | Reuse read-only rules normalization. This change accepts native market objects and common market / result / data envelopes, plus consolidated rules/PDF text when supplied. Do not launch another API client or add an MCP runtime dependency. |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Market search, order book, portfolio and trading tool schema | Market reads and portfolio/execution already have owners in KalshiClient and engine; this MCP also exposes order-write tools. | Do not connect a second order writer. Any future tool bridge must be read-only or call the existing guarded execution path. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Independent probability estimates, edge against live book, risk gates and fractional-Kelly sizing | risk.decide, fee calculations, book-aware fill planning, caps, guard, calibrated signals and held-out model promotion already implement these responsibilities. | Keep those owners. Treat external formulas as candidates for offline comparative tests only; never replace current sizing or caps by copying another bot. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory-skewed quotes and Avellaneda–Stoikov market-making math | This project is a directional sports / 15-minute crypto strategy, not a two-sided inventory market maker. | Do not import its order loop. Its inventory and quote concepts only become relevant if a separately approved market-making strategy is built and backtested. |
| NWS / GFS weather feeds | Forecasts and observation context for weather contracts | Current supported focus is sports and 15-minute crypto; weather settlement would need an explicitly supported market family and official settlement mapping. | Not added. Revisit only when weather markets are in scope. |
| FRED economic data | Macro series for economic contracts | No current macro-contract strategy/settlement map exists. | Not added; avoid unused collectors and stale signals. |
| FIX 4.4 | Standardized execution protocol | The current bot uses Kalshi's existing REST client and guarded order path; FIX eligibility and broker/exchange support have not been verified. | Not enabled. Requires documented account support, reconciliation, idempotency, failure tests and latency benchmarks first. |

## Changes in this PR

- bot/market_rules.py now normalizes direct Kalshi market objects and common read-only MCP response envelopes (market, result, data).
- It accepts bounded consolidated rule text fields (rules_text, rules_pdf_text, contract_terms_text) and series prohibitions when an upstream read-only source supplies them.
- Missing rule text remains explicitly unavailable. The helper does not fetch third-party data, assert a PDF was verified, infer missing settlement rules, or authorize an order.
- Tests cover nested envelopes, consolidated text, size bounds and fail-closed behavior.

## Integration boundaries

- No UI changes, new runtime services, dependencies, collectors, API keys, or order routes.
- OmniRoute → LiteLLM → OpenRouter stays the existing optional LLM route. OpenRouter-specific web search remains on OpenRouter.
- Existing Kalshi rate limiting, short-lived metadata caching, uncached price/order-book reads, fees, fractional Kelly, exposure limits, circuit breakers and final risk rechecks remain authoritative.
- External pages and MCP results are untrusted research data, not instructions. Contract text can inform counsel, but cannot override numeric risk checks.
- “Flawless” cannot be promised from source inspection alone. CI and paper-mode cycles must verify the change against the actual repository runtime before deployment.


## Follow-up integration: series-level settlement metadata

The existing review was extended without replacing its MCP-envelope normalization:

- The bot's current `KalshiClient` now has a read-only `series(ticker)` method. It uses the same request pacing and the existing `/series` metadata cache.
- The existing candidate review fetches series details only for the already-shortlisted sports candidate and passes them to `sports_counsel`. Lookup failure does not create synthetic rules or override the counsel gate.
- `summarize_rules(market, series=...)` combines market-specific text with series-level settlement sources, additional prohibitions, and document URLs. It flags prohibitions for manual review and exposes only HTTPS URLs on Kalshi domains or the known Kalshi public-docs S3 bucket pattern.
- A document URL is a reference only. This does not claim that a PDF has been downloaded, parsed, or independently verified. PDF extraction remains a separate, bounded follow-up if it can be implemented without introducing an unneeded runtime dependency or unbounded network work.
- New tests cover series lookup validation, series restrictions, and a spoofed AWS hostname. Existing MCP envelope tests remain intact.
