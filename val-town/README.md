# Val Town Kalshi API bridge

This is an additive, private, read-only bridge for public Kalshi market metadata. It is designed for the existing `dtmiescke/kalshi-bot`; it does not replace the bot and complements the existing Cloudflare read-only WebSocket relay.

## Configure
1. Create a private TypeScript HTTP val in Val Town and paste `index.ts`.
2. Add a Val Town secret named `ATI_INGEST_SHARED_TOKEN` with a long random value.
3. Set `VAL_TOWN_KALSHI_URL` to the val HTTP URL and `ATI_INGEST_SHARED_TOKEN` to the same value in the bot deployment environment.
4. Keep both values out of commits, logs, browser code, and public issues.

## What goes through the bridge
Public GET catalog/reference reads: events, markets, series, exchange status, and historical market metadata. The bridge has an allowlist, requires a bearer token, bounds query and response sizes, and forwards no Kalshi credentials.

## What stays direct
Live WebSockets and ticker updates can use the existing Cloudflare read-only relay for fanout to dashboards, while the bot retains its direct Kalshi WebSocket/client as the authoritative feed and fallback. Order books used for execution, private account/position/order/fill endpoints, and every create/cancel/amend order request stay direct in the bot. Do not add portfolio or write endpoints to the bridge.

## Free-tier limits and fallback
This is request-driven, not a continuous worker or WebSocket relay. Free-tier quotas and runtime limits can change. If bridge calls fail, the bot must fall back to its direct Kalshi client; errors must never become an empty market list. Historical snapshots and bet outcomes remain in the bot persistent store.

Run regression and paper/demo smoke tests before using this in a trading decision path. This bridge does not validate live-money readiness.
## Three-service layout

- **Val Town:** public reference and catalog GET requests, with direct Kalshi fallback.
- **Cloudflare:** existing `realtime/kalshi-feed/` Worker + Durable Object, read-only WebSocket relay for live ticker/orderbook-delta/trade updates and dashboard fanout. Deploy it separately using its existing workflow and secrets. No order placement.
- **Bot:** decision engine, historical storage, risk limits, authenticated account and order operations, and direct Kalshi feed fallback. Do not interpret a relay health response alone as proof that market data is fresh.

These services are complementary rather than redundant: Val Town is not a real-time relay; Cloudflare does not replace the metadata client or trading engine. Do not copy the Val Town bearer token into dashboard JavaScript. Do not move trading credentials into either relay.
