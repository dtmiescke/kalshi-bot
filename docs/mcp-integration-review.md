# MCP and external-engine integration decisions

The existing bot remains the sole trading system. Do **not** run third-party
Kalshi order-writing MCP tools alongside `KalshiClient` or grant LLMs order
permissions. No UI, market universe, risk threshold or live-mode setting changes.

## What is implemented

`bot/mcp_research_bridge.py` provides an **in-process, read-only MCP-style
research adapter**. It is not a network MCP server and is not yet connected
to a model tool-calling loop. It reuses the existing Kalshi client for public
market metadata and bid-depth observations and `contract_pdf.py` for bounded,
allowlisted official contract-document extraction. It has three allowlisted
research operations, rejects unknown tools and malformed tickers, and exposes
no write or portfolio functions. Evidence must still be checked for freshness
and contract applicability before the existing counsel can use it.

## Reviewed upstream concepts

- [9crusher/mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi):
  useful contract-rule lookup and normalized market-data tool design; adapt
  read-only concepts rather than adding a second Kalshi client.
- [BrainDAO/mcp-kalshi](https://github.com/BrainDAO/mcp-kalshi):
  market/orderbook and account tooling overlap existing client. **Do not
  install order or wallet actions** in an AI-accessible server.
- [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli):
  compare bankroll and Kelly methods offline; current fee-inclusive Kelly,
  liquidity checks, position limits and circuit breakers remain authoritative.
- [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker):
  market-making inventory/quoting strategy is not a drop-in improvement to
  directional sports and 15-minute crypto trades. No quoting bot is started.
- NWS/GFS and FRED: defer until weather or economic contracts are explicitly
  enabled with official settlement source, data timestamps, and tests.
- FIX 4.4: no deployment without verified Kalshi/account support, recovery,
  reconciliation and measured benefit over the existing API.

OmniRoute → LiteLLM → OpenRouter is preserved. The bridge does not
create another LLM gateway, data collector, or execution path.

## Remaining validation before production wiring

1. Confirm official Kalshi API response fields and contract PDF behavior on
   real samples; test freshness, missing fields, partial PDF and rate limits.
2. If model tool calling is introduced, connect **only** the three allowlisted
   research operations through existing budgets and the counsel evidence path.
3. Run the repository regression suite and repeated paper cycles, then
   review the PR. No automatic live-trading activation or profit claims.
