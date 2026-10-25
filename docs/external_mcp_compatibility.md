# External Kalshi research integrations — compatibility review

This is an additive, read-only integration for the **existing** Kalshi bot.
The bot's `KalshiClient`, risk engine, persistence, and dashboard remain the
single authorities. No external MCP server is installed or given API keys.

## Reviewed projects and decisions

- **9crusher/mcp-server-kalshi**: market discovery, order books, recent trades,
  series metadata, and exact contract-PDF research are useful. Existing
  `KalshiClient` and `contract_pdf` already implement the underlying calls.
  `bot/kalshi_research_tools.py` now presents a bounded read-only facade
  reusing those implementations; it does not copy the MCP server or expose
  its create/amend/cancel-order tools.
- **BrainDAO/mcp-kalshi**: overlaps with the same market and portfolio APIs.
  Do not add a second HTTP client, credentials, storage, or order route.
- **OctagonAI/kalshi-trading-bot-cli**: useful ideas include provenance-aware
  edge assessment, risk-gate reporting, calibration, and correlation-aware
  exposure analysis. Existing risk and learning remain authoritative; do not
  import its separate Bun runtime, Octagon API dependency, half-Kelly sizing,
  or order execution without isolated time-split testing.
- **rodlaf/KalshiMarketMaker**: inventory caps, rate-limit/backoff discipline,
  and shutdown reconciliation are useful engineering references. Its active
  quoting workers, independent order lifecycle, and Avellaneda–Stoikov model
  are not suitable drop-in components for the existing directional bot.
- **NWS/GFS and FRED**: defer new collectors until weather or macro contracts
  are explicitly selected and settlement/freshness mappings are validated.
- **FIX 4.4**: do not add a second execution protocol without documented
  account eligibility, exchange support, reconciliation, and failure testing.
- **OmniRoute / LiteLLM / OpenRouter**: existing fallback chain is preserved;
  research adapter does not make LLM calls.

## Integration boundary

`KalshiResearchTools(client, store)` wraps an *existing* `KalshiClient`.
Available operations: `market`, `series`, `orderbook`,
`recent_trades`, `sports_events`, and `contract_rules`.
All use bounded ticker inputs. The contract rules call reuses the existing
trusted-host, size-bounded PDF extractor and cache. A missing PDF remains
unknown rather than becoming a fabricated settlement rule.

This is a callable internal research interface, **not a running MCP server**.
No scheduler, trading decision, dashboard, risk threshold, API secret, or
execution path is changed by this PR. Before wiring new agent consumers into
the scheduled engine, measure API-call budgets, latency, and source freshness
under paper mode and prove no duplicate fetches or stored observations.
