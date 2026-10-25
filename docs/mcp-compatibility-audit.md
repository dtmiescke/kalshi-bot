# External MCP and strategy compatibility audit (2026-10-25)

This is an **integration inventory**, not permission to install third-party trading software or enable live orders. The bot's existing KalshiClient, research pipeline, SQLite store, risk engine, and UI remain authoritative. External repositories are **untrusted reference implementations**. Review their licenses and code before copying any code.

## Current overlap verified against `main`

- `bot/kalshi_client.py`: paginated sports event/series discovery, market/series metadata, Kalshi order book and trades, portfolio reads, and guarded order methods. It already has a shared rate limiter and short-lived market-list cache; order books and trades are never cached as fresh prices.
- `bot/contract_pdf.py`: allowlisted HTTPS contract-terms PDFs, bounded download, no redirects, strict PDF parsing, source URL and timestamp, and URL-aware caching.
- `bot/llm.py`: OmniRoute → optional LiteLLM → OpenRouter chat fallback and request budgets; OpenRouter-specific web tools remain provider-specific.
- Existing contract rules and combo tests: `tests/test_contract_pdf.py`, `tests/test_contract_rules_safety.py`, `tests/test_combo_research.py`.

**Do not introduce a second Kalshi client with independent API pacing, a second order writer, a second SQLite ledger, or an unbounded LLM/MCP agent loop.**

## External candidates and decisions

| Reference | Useful concepts | Decision / reason |
|---|---|---|
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Unified market/rules research, exact PDF rules, exchange status/schedule, orderbook/candlestick reads | **Reuse patterns, not the executable MCP server.** Market, series, orderbook, trades, and PDF evidence are already present. Check whether exchange schedule and candlesticks fill a real gap before adding narrow read-only methods to the existing client. Its order tools must never be exposed to AI agents. |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Portfolio and order status reporting, market depth and price normalization | **Do not install as a parallel gateway.** The existing client already handles market and portfolio reads and orders. Only add missing read-only reconciliation/reporting endpoints after confirming exact data schemas and tests. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Candidate fractional-Kelly / risk-model experiments | **Research-only.** Compare against existing fee-aware sizing, exposure caps, drawdown limits, and fill assumptions in time-split shadow tests. No imported sizing constants or order path. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory-aware quoting / Avellaneda–Stoikov research | **Not a direct fit** for the current directional sports/15-minute crypto bot. Requires dedicated quoting lifecycle, inventory limits, fee and adverse-selection models; do not enable. |
| NWS / GFS weather data | Official weather forecast/observation evidence | **Defer** unless weather markets are explicitly supported; settlement station, revision, issue time, and latency must be modeled. |
| FRED macro data | Official economic releases and vintages | **Defer** unless macro contracts are supported; enforce point-in-time vintages to avoid backtest leakage. |
| FIX 4.4 | Potential institutional execution transport | **Do not activate** without account eligibility, supported endpoints, full reconciliation, and demonstrated advantage over existing REST client. |

## Safe integration boundary

```text
Existing event/market discovery
    -> existing KalshiClient (one request budget/rate limiter)
    -> existing settlement metadata + contract_pdf (source-aware evidence)
    -> existing sports/crypto researchers and LLM budgeted council
    -> existing risk and freshness checks
    -> existing KalshiClient order methods (sole execution authority)
    -> existing ledger, grades, dashboard, janitors and watchdog
```

An optional future **read-only MCP façade** may expose curated snapshots **from the existing services**, not launch a second Kalshi client or receive credentials. Its allowlist may include `market_summary`, `series_rules`, `orderbook_snapshot`, `trade_tape_summary`, and `portfolio_summary` only if those methods reuse existing services and enforce caller authorization for private portfolio reads. Do **not** expose `create_order`, `cancel_order`, `amend_order`, `decrease_order`, arbitrary HTTP fetch, shell, file access, or key material. An LLM may suggest research but cannot call order methods.

## Engineering gates before accepting a feature

1. Demonstrate a **missing capability** against existing modules; reject duplicates.
2. Identify a canonical source and time of observation. Explicitly mark unknown, stale, and truncated data; do not convert them to confidence.
3. Reuse existing client, retries, pacing, store, AI budget, and incident logging. Avoid new credentials and recurring cost unless separately approved.
4. Validate YES/NO semantics, dollars versus cents, orderbook sides, fractional quantities, and settlement wording against fixtures and the current Kalshi API.
5. Add offline regression tests for invalid tickers, empty/malformed API data, timeouts/429s, stale rules/PDF URLs, pagination and deduplication.
6. Compare predictions and fee-adjusted outcomes using walk-forward, point-in-time shadow tests; separate placed trades from hypothetical opportunities.
7. Prove **zero changes** to live-mode activation, execution/risk gates, existing UI, and learned/ledger history. Run full CI before merge.
8. No third-party code copied without a license and security review; no unattended dependency installation.

## Immediate next work (not claimed complete)

- Audit exchange status/schedule and candlestick access against existing client consumers, then add **only** missing read-only calls with fixtures and call-site integration.
- Audit order/fill/settlement reconciliation coverage, without changing write APIs.
- Evaluate external sizing/market-making research offline only; do not promote models without measurable out-of-sample improvement.

This inventory deliberately makes **no claims of flawless operation, profit, third-party code execution, or successful live-market validation**.
