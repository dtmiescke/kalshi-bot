# Kalshi API compatibility additions

This is an additive integration; it does not replace the existing bot or switch existing order callers to a new endpoint.

## Added in this change

- `KalshiClient.market_orderbooks(tickers)`: reads up to 100 market books in one request to `GET /markets/orderbooks?tickers=...`, normalizes dollar/cents-style price values to dollar floats, sorts levels best-first, and rejects invalid tickers before making a request.
- `KalshiClient.place_order_v2(...)`: isolated opt-in client method for the documented V2 `POST /portfolio/events/orders` endpoint. It validates inputs, emits fixed-point dollar prices and contract counts, and includes a unique client order ID and explicit self-trade prevention.
- Unit tests use mocks only; they do not place orders or contact Kalshi. Bulk refresh integration tests cover chunk-compatible refresh, missing-row fallback, malformed-data rejection, and skipping fresh WebSocket books.

## Why V2 is not automatically wired into the existing engine

The existing engine and ledger use legacy YES/NO order methods and response assumptions. Kalshi's V2 schema uses the YES-book `bid` / `ask` side and fixed-point dollar prices. Automatically swapping the path without migrating the complete order, cancellation, fill, position-reconciliation, and settlement flow would be unsafe. The new method is deliberately separate until those call sites and response mappings have end-to-end demo coverage.

## Remaining implementation / verification checklist

- [ ] Migrate order create, get, cancel, amend, and fills together to V2 after reviewing every engine and recovery call site.
- [ ] Add order-group integration only after confirming current API schema, group limit semantics, and the desired exchange-side limits; keep existing local risk checks authoritative.
- [x] Connect bulk orderbook retrieval to bounded stale/resync refresh in chunks of at most 100; fallback to per-market REST for missing/failed rows and reject malformed snapshots. Fresh WebSocket books are skipped in quiet mode.
- [ ] Add a freshness gate for sports live data (score/status/clock), and ensure missing or unsupported game statistics cannot be treated as current.
- [ ] Verify WebSocket reconnect/resubscribe, sequence-gap resync, keepalive, and stale-feed no-trade behavior with deterministic tests.
- [ ] Verify historical cutoff, archive pagination, duplicate trade IDs, and settlement-result handling with recorded fixtures.
- [ ] Record API response status, Retry-After, rate-limit headers, and request volume without logging credentials or sensitive account data.
- [ ] Run the full regression suite and paper/demo smoke tests. Never enable live mode as part of this change.

## Official references

- API index: https://docs.kalshi.com/llms.txt
- V2 create order: https://docs.kalshi.com/api-reference/orders/create-order-v2
- Bulk market orderbooks: https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks
- Game stats: https://docs.kalshi.com/api-reference/live-data/get-game-stats
- WebSocket quick start: https://docs.kalshi.com/getting_started/quick_start_websockets
- Rate limits: https://docs.kalshi.com/getting_started/rate_limits
- Historical data: https://docs.kalshi.com/getting_started/historical_data
