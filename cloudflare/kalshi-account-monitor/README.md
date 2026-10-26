# Kalshi account monitor (on-demand)

This is an optional read-only Cloudflare Worker for the existing Kalshi Bot HQ dashboard. It is **not** a scheduler and has no cron trigger. The dashboard calls it when the Account tab is opened/refreshed and while the tab remains visible; it stops polling when the page is hidden.

## Deploy

1. Create a Worker in Cloudflare and deploy `worker.js` (or deploy this folder with Wrangler).
2. Add Worker secrets:
   - `KALSHI_API_KEY_ID`: a Kalshi API key ID with read-only permissions if Kalshi offers that permission for your account.
   - `KALSHI_PRIVATE_KEY`: the matching PKCS#8 PEM private key. Never put it in the browser, GitHub, or chat.
3. Add a plain-text Worker variable `APP_ORIGIN` equal to the exact HTTPS origin serving Bot HQ. Do not include a path or trailing slash.
4. The worker defaults to Kalshi's recommended production Trade API base, `https://external-api.kalshi.com/trade-api/v2` (the legacy `api.elections.kalshi.com` host may also be supported). Set `KALSHI_API_BASE_URL` only when deliberately targeting another compatible environment.
5. Set the Worker URL in Bot HQ → Account and use the base Worker URL, not `/api/account`.
6. Test `/health`, then open Account in Bot HQ and verify balance, positions, fills and settlements load.

## API behavior

- `GET /health`: service health only.
- `GET /api/account`: reads Kalshi balance, positions, fills and settlements using signed GET requests.
- CORS only permits the configured `APP_ORIGIN`; non-GET requests are rejected.
- No credentials or account response is cached by the Worker.
- The UI distinguishes portfolio value from realized profit. Settlements/fills are raw account records; do not claim realized P&L or win rate unless those fields can be reliably reconciled after fees, reversals and deposits/withdrawals.
- This Worker is a reporting endpoint only. It has no order-placement routes and must not be given trading permissions if a read-only key is available.

## Important limitation

The Worker must be deployed and configured by the account owner. Until the Cloudflare URL and secrets are configured, the dashboard cannot show live account data from this endpoint.
