# MCP and external-system compatibility audit (2026-10-25)

This document records source-reviewed compatibility decisions for the **existing** Kalshi bot. It is not permission to install untrusted servers, add live order paths, or enable new contract classes.

## Single-owner architecture

| Capability | Existing owner | Third-party technique worth reusing | Decision |
| --- | --- | --- | --- |
| Market/event/series discovery | `bot/kalshi_client.py`, existing sports discovery | 9crusher/BrainDAO read-only discovery APIs | **Reuse existing client**, not a second polling client |
| Contract rules and official PDFs | `bot/market_rules.py`, `bot/contract_pdf.py` | 9crusher consolidated rules, PDF evidence | **Already implemented**; extend only with demonstrated missing settlement fields |
| MCP tool response formats | `bot/market_rules.py` bounded nested envelope parser | structuredContent and JSON text blocks | **Already implemented**; no MCP server runtime required |
| Book and price conversion | `bot/kalshi_client.py`, `bot/risk.py` | 9crusher cents/YES-NO conversion | **Do not duplicate**; use existing canonical dollar pricing and bid/ask mirror |
| Portfolio/orders | `bot/kalshi_client.py` and existing execution guards | BrainDAO account tools | **Do not install order-write tools**; avoid bypassing risk, reconciliation and paper-mode gates |
| Risk sizing | `bot/risk.py`, `bot/config.py` | OctagonAI Half-Kelly comparison | **Research only**: existing fractional Kelly is configured at 0.25 by default; do not silently raise risk |
| Market making | existing maker-order controls | Avellaneda–Stoikov ideas | **No deployment** without inventory/fee/fill calibration, time-split replay and order-reconciliation tests |
| AI routing | `bot/llm.py` | OmniRoute, LiteLLM, OpenRouter | **Already integrated**; preserve existing budget and provider-specific web tools |
| NWS/GFS and FRED | no approved weather/macro market rollout | official observation/economic data | **Deferred** until settlement mapping, timestamped freshness, licensing and explicit market opt-in |
| FIX 4.4 | existing REST execution | low-latency exchange integration | **Deferred** pending official entitlement, session semantics, reconciliation and latency evidence |

## Source material reviewed

- https://github.com/9crusher/mcp-server-kalshi — supports discovery, consolidated settlement rules, official PDF retrieval, orderbook/trades, authenticated portfolio and order mutation; defaults to demo but can target production. Its confirmation preview does **not** replace this bot's execution guard.
- https://github.com/BrainDAO/mcp-kalshi — market data and account/order MCP tools; installing the server with credentials creates an independent order-capable agent interface.
- Existing files examined: `bot/llm.py`, `bot/config.py`, `bot/market_rules.py`, `bot/contract_pdf.py`, `bot/kalshi_client.py`, `bot/risk.py`, and `tests/test_contract_pdf.py`.

## Integration contract for any future MCP tool

1. **Read-only allowlist:** accept only market, event, series, rules, trades, candles and orderbook reads. Reject any create/amend/cancel/decrease-order method even if a server advertises a `confirm` flag.
2. **One data owner:** invoke the existing KalshiClient wherever an equivalent method exists. Do not create an additional unaudited fetch loop, persistence table or credential store.
3. **Canonical units:** normalize integer cents and dollar-string fields using the existing `price()` helper. Keep YES and NO sides explicit; never treat the bid as the executable ask.
4. **Settlement evidence:** retain URL/provenance and original rule text, use bounded extraction and fail closed for absent or contradictory settlement sources.
5. **Freshness:** never substitute cached prices for current executable quotes; respect global Kalshi pacing, API errors and retry-after.
6. **Separation of duties:** MCP/LLM evidence can advise, never directly submit orders or increase stake/risk limits.
7. **No new market classes by default:** weather/macro require separate opt-in and a tested mapping to the exact Kalshi settlement definition.
8. **Promotion gate:** add deterministic fixtures for provider response shapes, malformed data, stale evidence, rate limiting, fees, and risk invariants; then run full CI and repeated paper cycles before any operational use.

## Follow-up verification required

- Confirm whether any current rules field is missing from the native market/series payload before writing new adapters.
- Compare third-party price conversion fixtures with `bot/kalshi_client.py` and `bot/risk.py` in tests; reject discrepancies rather than choosing a second interpretation.
- Benchmark paper-only fill simulations before considering alternative risk or maker quoting.
- Audit CI and runtime metrics for evidence freshness, duplicate observations and execution-path invariants.

**This audit intentionally makes no production runtime, UI, trading, secrets or dependency changes.** It avoids an untested second exchange client while making the integration boundaries explicit.
