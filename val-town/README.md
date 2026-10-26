# Val Town Kalshi API bridge

This is an additive, private, read-only bridge for public Kalshi market metadata. It is designed for the existing `dtmiescke/kalshi-bot`; it does not replace the bot and does not use Cloudflare.

## Configure
1. Create a private TypeScript HTTP val in Val Town and paste `index.ts`.
2. Add a Val Town secret named `ATI_INGEST_SHARED_TOKEN` with a long random value.
3. Set `VAL_TOWN_KALSHI_URL` to the val HTTP URL and `ATI_INGEST_SHARED_TOKEN` to the same value in the bot deployment environment.
4. Keep both values out of commits, logs, browser code, and public issues.

## What goes through the bridge
Public GET catalog/reference reads: events, markets, series, exchange status, and historical market metadata. The bridge has an allowlist, requires a bearer token, bounds query and response sizes, and forwards no Kalshi credentials.

## What stays direct
Live WebSockets, ticker updates, order books used for decisions, trades, private account/position/order/fill endpoints, and every create/cancel/amend order request stay on the existing bot direct client. Do not add portfolio or write endpoints to the bridge.

## Free-tier limits and fallback
This is request-driven, not a continuous worker or WebSocket relay. Free-tier quotas and runtime limits can change. If bridge calls fail, the bot must fall back to its direct Kalshi client; errors must never become an empty market list. Historical snapshots and bet outcomes remain in the bot persistent store.

Run regression and paper/demo smoke tests before using this in a trading decision path. This bridge does not validate live-money readiness.