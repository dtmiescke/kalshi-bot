# External Kalshi component review (integrated in place)

This review uses public repository documentation and the current bot architecture.
It does not claim that third-party software has been installed, independently security-audited,
or proven profitable.

## Incorporated

- **Shared read-only rules summary:** `bot/market_rules.py` is the single source of
  truth for bounded settlement-rule context. The crypto settlement pipeline now attaches
  that same summary to its existing contract-term record. Sports counsel already consumes
  the helper. No parallel rule parser or duplicate data store is introduced.
- **LLM routing:** the existing AI client now supports optional LiteLLM as the middle
  fallback: OmniRoute → LiteLLM Proxy → OpenRouter. Shared call budgets and the existing
  OpenRouter-specific web-search path remain intact.
- **One execution authority:** no second Kalshi client, MCP runtime, portfolio manager,
  or order-writing path is added. Existing deduplication, price freshness, risk checks,
  reconciliation, and guarded execution remain authoritative.

## Findings and decisions

| Component | Reusable idea | Decision |
|---|---|---|
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Contract rules, settlement-source and early-close review, legal PDF extraction | Reuse official market rule fields via the in-process shared summary. This feature does **not** fetch the legal PDF. Do not enable the MCP order-writing tools. |
| [IQAI/BrainDAO mcp-kalshi](https://github.com/IQAIcom/mcp-kalshi) | Market discovery, order book, portfolio tools | Existing Kalshi client already covers discovery and order-book needs. Portfolio/order tools would create a competing authority, so they are not wired in. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Multiple risk gates, correlation/concentration checks, independent probability and edge analysis | Keep as a benchmark for review. Do not copy the separate bot or its external API requirement. Existing fee-aware sizing and limits stay in force until alternatives pass out-of-sample tests. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory-aware market making | Not adopted now: market making is a different strategy from the bot's current sports and short-horizon crypto approach; it needs queue-position, fill, inventory and fee simulation. |
| NWS/GFS and FRED | Weather/macro fundamentals | Not connected because the current focus is sports and crypto. These feeds need supported market types, settlement-source mapping, freshness and event-time controls first. |
| FIX 4.4 | Low-latency execution | Not activated without demonstrated account/protocol compatibility and reconciliation benefit. The existing guarded Kalshi REST path remains the sole execution authority. |
| LiteLLM | Optional OpenAI-compatible gateway/proxy | Incorporated in the existing LLM routing layer; no second orchestration stack. |

## Safety and limits

- Rule summaries are advisory metadata, not a legal interpretation or trade signal.
- Keyword flags can be false positives; absence of a flag does not prove a clause is absent.
- A rules summary does not fetch or verify the official contract PDF.
- Missing or ambiguous settlement terms remain unavailable/review-required, not silently safe.
- This integration does not change the UI, risk limits, strategy weights, paper/live mode, or order execution.
