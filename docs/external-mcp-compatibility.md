# External Kalshi MCP and research integration audit

Status: **reviewed architecture; no third-party server installed or granted trading credentials**.

This document records what to adopt from the four reviewed repositories without replacing the existing sports/crypto bot. It is an engineering compatibility decision, not a claim that an external trading system has been integrated.

## Single-owner invariants

1. `KalshiClient` and the existing execution/risk pipeline are the **only** order authority. No external MCP process may submit, amend, cancel, liquidate, or independently reconcile trades.
2. Existing sports and 15-minute crypto collectors own market snapshots. Add a new observation only if its information is absent and it has a unique source, market identifier, observed timestamp, and freshness policy. Do not duplicate book polling or ledger entries.
3. The existing `bot/contract_pdf.py` already downloads and extracts official contract PDFs with an HTTPS host allowlist, bounded bytes/pages, disabled redirects, and cache invalidation when the URL changes. Reuse this rather than importing another PDF downloader.
4. The existing `bot/llm.py` routes OmniRoute → optional LiteLLM → OpenRouter. External tools are not allowed to bypass its budgets or inject their own unrestricted model calls.
5. Third-party research is **evidence**, never a trade instruction. Missing, stale, inconsistent, or ambiguous contract rules must be reported as unknown, not filled in by an LLM.
6. Paper/shadow results must never be merged into the placed-bet ledger or counted as actual wins. No UI replacement.

## Reviewed repositories and decisions

| Project | Reusable idea | Decision and required validation |
| --- | --- | --- |
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | `get_market_rules` combines primary/secondary rules, settlement source, early-close clauses and prohibitions; `fetch_rules_pdf` supplies legal source text | **Adopt semantics, not its server.** Compare its rule-field coverage against the existing Kalshi series/market metadata and `contract_pdf.py`. Add missing fields to the existing read-only research object only after fixture-based checks. Do not import its `create_order`, `amend_order`, `decrease_order`, `cancel_order` tools. |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Market discovery, order-book and account/settlement read models | **Use as a comparison reference only.** Existing Kalshi market-data and portfolio paths already own these domains. Its order, cancel and batch-cancel tools are excluded. Verify its YES/NO quote conversions against official API responses before borrowing any normalization behavior. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Independent edge explanation, correlation/concentration diagnostics, five-gate risk-review approach | **Evaluate offline only.** Existing fractional-Kelly sizing and risk caps remain authoritative. Half-Kelly is not inherently better; compare fee-adjusted expected value, calibration, drawdown and time-split performance on identical historical opportunities before proposing a change. Its execution CLI and paid Octagon research dependency are not added. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory awareness, spread/liquidity selection, stale-order cleanup and reconciliation checks | **Borrow monitoring concepts, not market-making workers.** Existing maker-order lifecycle and position caps remain the owner. Independent quoting, inventory workers, cancel-all, liquidation and live deployment are excluded. |

## Other suggested services

- **NWS / GFS:** only consider for explicitly enabled weather contracts, with documented forecast issuance time, observation time, geographic mapping and the contract's official settlement source. Not part of the current sports/crypto default.
- **FRED:** only consider for explicitly enabled macroeconomic contracts, with release-vintage/revision handling and settlement-source verification. Do not use revised series as if known at decision time.
- **FIX 4.4:** not enabled. Confirm exchange and account eligibility, protocol availability, sequence recovery, order reconciliation and rollback before any design proposal.
- **OmniRoute / LiteLLM / OpenRouter:** retain the existing route and call budgets. No new model gateway or paid service is required by this audit.

## Implementation checklist for a future code PR

1. Build an offline fixture set for `rules_primary`, `rules_secondary`, series contract URL, early close, settlement source, and ambiguous/conflicting terms.
2. Inspect the existing market/series data client and research call sites; extend the existing normalized research record rather than adding another collector or MCP process.
3. Attach provenance: Kalshi market ticker, series ticker, document URL, retrieved time, source field, and explicit freshness/unknown status. Preserve raw evidence separately from inferred summaries.
4. Add unit tests for missing terms, malformed URLs, oversized PDFs, redirect attempts, changed PDF URLs, source conflicts, and stale cache; ensure all failure paths leave order authority unchanged.
5. Compare fee-aware probability, YES/NO conversions, order-book units, and correlation diagnostics with historical fixtures; promote no new sizing logic without out-of-sample evidence.
6. Run repository tests and scheduled-workflow checks. Review PR before merge; keep live-trading approvals unchanged.

## Security and licensing

Do not copy third-party source wholesale or assume its license grants unrestricted reuse. Verify current license, dependencies and maintenance status for any code actually borrowed. Never pass Kalshi signing keys to third-party MCP tools, external LLMs or an independent worker. Any eventual read-only adapter should have no order-writing methods, should use the existing client's public-data path, and should have bounded timeouts, rate limits, and explicit provenance.
