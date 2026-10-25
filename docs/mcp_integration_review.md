# MCP and external-engine compatibility review

This is an additive, read-only integration. Existing sports/crypto research, UI,
OmniRoute → LiteLLM → OpenRouter, budget limits, KalshiClient, risk engine,
ledger, and paper/live gates remain authoritative.

## Evaluated candidates

| Candidate | Useful feature | Decision |
| --- | --- | --- |
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Market/series contract rules and source provenance | Adapt market/series metadata through existing KalshiClient.market_rules(); no MCP subprocess or order tools |
| [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi) | Order book, market discovery, portfolio analytics | Existing KalshiClient already handles these; no duplicate feed, signing key, or execution route |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Kelly/risk methodology | Compare only in offline time-split evaluation; do not change live risk sizing |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory-aware market-making concepts | Not applicable to existing directional sports/crypto engine without independent simulations |
| NWS/GFS and FRED | Weather/macro data | Deferred: current sports and crypto market scope; no unrequested market expansion |
| FIX 4.4 | Alternative execution | Not added; would bypass guarded REST order path without reconciliation and eligibility proof |

## Read-only contract research

`KalshiClient.market_rules(ticker)` retrieves the market and its series using
the existing rate-limited API client, returns original metadata alongside
normalized rule fields, and flags `complete=False` when primary rules or a
settlement source is absent. The flag is a **research completeness indicator**,
not permission to trade. Callers must treat absent metadata as unknown, never
fabricate contract terms or assume market eligibility.

PDF fetching/extraction is intentionally **not** included yet: external
documents require source URL validation, bounded content handling, text
extraction dependencies, and freshness/provenance checks. The original market
and series responses are preserved so future research can add verified PDF
handling without schema loss.

No new credentials, external services, dependencies, trade triggers, or
scheduled requests. The adapter is opt-in to research callers; no decision
weights or execution logic are changed.

## Validation

`python -m unittest tests.test_contract_rules_mcp`

The tests assert read-only GET access, malformed ticker rejection, metadata
normalization, and incomplete-rule handling. Full repository CI is required
before merging. No profit, latency, or live-order guarantees are implied.
