# Kalshi read-only real-time feed relay

This is a separate Cloudflare Worker + Durable Object service. It does not replace the existing bot or its GitHub Pages dashboard and does not contain order-placement endpoints.

## Required secrets

Set these in the GitHub repository settings for `dtmiescke/kalshi-bot`:

- `CLOUDFLARE_API_TOKEN`: Cloudflare API token with Workers Scripts edit and account access needed for Workers Builds/deployments and Durable Objects.
- `CLOUDFLARE_ACCOUNT_ID`: the target Cloudflare account ID.

The deploy workflow securely syncs the existing GitHub Actions secrets `KALSHI_DATA_KEY_ID` and `KALSHI_DATA_PRIVATE_KEY` into Cloudflare using Wrangler. You do not need to paste the Kalshi private key into Cloudflare manually. If either existing GitHub secret is missing, deployment stops before publishing.

Use the existing Kalshi data-only key where possible. Do not paste private keys into chat, issues, commits, or workflow files. The relay uses the production market-data WebSocket and restricts client commands to read-only `ticker`, `orderbook_delta`, and `trade` channels. It rejects account/order-entry channels.

## Deploy

Once the two Cloudflare GitHub secrets are configured, run **Actions → Deploy Kalshi Read-only Feed → Run workflow**, or push a change under this folder. The workflow dry-runs the configuration and then deploys it.

## Verify

- `https://kalshi-readonly-feed.<your-workers-subdomain>.workers.dev/health` should return JSON with `ok: true`.
- `credentialsConfigured` must be true after the Worker secrets are set.
- Connect a WebSocket client to `wss://kalshi-readonly-feed.<your-workers-subdomain>.workers.dev/ws`, send a Kalshi subscribe command for a known active market's `ticker` channel, and confirm fresh ticker messages arrive.

The published GitHub Pages dashboard must be wired to the deployed URL after deployment; it must never contain Kalshi credentials. This service only streams market data and does not place bets or change paper/live mode.
