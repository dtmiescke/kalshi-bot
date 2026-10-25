# External Kalshi component review (integrated in place)

This review is based on public repository documentation/source descriptions and the
existing `kalshi-bot` architecture. It is not a claim that third-party software has
been installed, run, security-audited, or proven profitable.

## What is incorporated now

- **Read-only contract-rule audit:** `bot/contract_rules.py` summarizes the official
  `rules_primary` and `rules_secondary` fields already returned by the existing
  Kalshi client. `bot/live/settlement.py` attaches this audit to the existing
  contract-term record. It identifies explicitly named settlement sources, flags
  rule text mentioning early-close language or terms that warrant review, and
  marks absent rules as unknown.
- **Single data/execution path:** no second Kalshi client, market collector, MCP
  runtime, portfolio manager, or order-writing path is added. Existing deduplication,
  price freshness, risk, order reconciliation, and guarded execution remain authoritative.
- **Risk research only:** external Kelly/market-making ideas are not allowed to change
  position sizing or trade eligibility merely because another repository recommends them.

## Findings and decisions

| Component | Reusable idea | Decision |
|---|---|---|
| [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) | Contract rules, settlement-source and early-close review, PDF extraction | Use existing official rule fields through the in-process audit. The legal contract PDF is **not** fetched by this feature; exact PDF verification remains a separate future task. Do not enable the MCP's order-writing tools. |
| [BrainDAO/IQAI mcp-kalshi](https://github.com/IQAIcom/mcp-kalshi) | Discovery, order-book and portfolio tools | Existing Kalshi client already provides market/order-book access. Portfolio and trade tools would create a competing authority, so they are not wired in. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) | Multiple risk gates, correlation/concentration checks, independent probability and edge analysis | Compare these concepts with existing guards and out-of-sample results before any change. Do not copy the separate bot or its external API requirement. Existing fee-aware sizing and limits stay in force. |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Inventory-aware market making | Not adopted for now: market making is a different strategy from this bot's current event/sports and short-horizon crypto approach and needs separate queue-position, fill, inventory and fee simulations. |
| NWS/GFS and FRED | Weather/macro fundamentals | Not connected: current focus is sports and crypto, and these feeds would need supported market types, settlement-source mapping, freshness and event-time controls first. |
| FIX 4.4 | Low-latency execution protocol | Not activated: no demonstrated account/protocol compatibility or reconciliation benefit; the current guarded Kalshi REST path remains the sole execution authority. |
| LiteLLM | Optional OpenAI-compatible gateway/proxy | Use only through the existing LLM routing layer when its implementation and CI are confirmed. Do not run a second model orchestration stack beside it. |

## Safety and limits

- Rule audit results are **advisory metadata**, not a legal interpretation or a trade signal.
- A keyword match can be false positive; absence of a keyword does not prove a clause is absent.
- A rule text audit does not fetch or verify the official contract PDF.
- Missing, ambiguous, or stale settlement terms must not be promoted to confident model inputs.
- This feature does not change the UI, risk limits, strategy weights, paper/live mode, or order execution.
