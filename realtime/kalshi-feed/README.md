# Kalshi read-only real-time feed relay

This is a separate Cloudflare Worker + Durable Object service. It does not replace the existing bot or its GitHub Pages dashboard and does not contain order-placement endpoints.

## Required secrets

Set these in the GitHub repository settings for `dtmiescke/kalshi-bot`:

- `CLOUDFLARE_API_TOKEN`: Cloudflare API token with Workers Scripts edit and account access needed for Workers Builds/deployments and Durable Objects.
- `CLOUDFLARE_ACCOUNT_ID`: the target Cloudflare account ID.

The deploy workflow prefers `KALSHI_DATA_KEY_ID` and `KALSHI_DATA_PRIVATE_KEY` (dedicated read-only credentials). For existing installations, it falls back to the bot secrets `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY`. It securely syncs the selected values into Cloudflare using Wrangler; never paste private keys into chat or commits. Cloudflare deployment still requires `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

Use the existing Kalshi data-only key where possible. Do not paste private keys into chat, issues, commits, or workflow files. The relay uses the production market-data WebSocket and restricts client commands to read-only `ticker`, `orderbook_delta`, and `trade` channels. It rejects account/order-entry channels, limits clients and message rates, and validates subscription inputs. **The `/ws` endpoint is publicly reachable unless you put it behind an access-control layer.** These limits reduce abuse but are not authentication; do not treat the feed as private or production-hardened until access is restricted at the edge.

## Deploy

The GitHub Actions workflow automatically runs on changes to this folder or its workflow file. You can also run **Actions → Deploy Kalshi Read-only Feed → Run workflow**. It validates the configuration, syncs secrets, and deploys the Worker. A green workflow means deployment succeeded; it does not by itself prove the upstream Kalshi handshake or ticker delivery works.

## Verify

- `https://kalshi-readonly-feed.<your-workers-subdomain>.workers.dev/health` should return JSON with `ok: true`.
- `credentialsConfigured` must be true after the Worker secrets are set.
- Connect a WebSocket client to `wss://kalshi-readonly-feed.<your-workers-subdomain>.workers.dev/ws`, send a Kalshi subscribe command for a known active market's `ticker` channel, and confirm fresh ticker messages arrive.

The published GitHub Pages dashboard must be wired to the deployed URL after deployment; it must never contain Kalshi credentials. This service only streams market data and does not place bets or change paper/live mode.
