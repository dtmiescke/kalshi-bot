# Sports Intelligence Upgrade

This document describes a free-first, read-only research collector for the existing application. It must preserve the current UI and keep live trading disabled during validation.

## Sources
- Official league and team feeds where available
- RSS feeds and publicly accessible sports reporting
- Existing odds provider within its free quota
- Kalshi's official market catalog, order books, trades, and settlement data
- Free public score/stat endpoints where permitted

## Reliability
Store source URL, observed time, published time, event ID, entity matches, confidence, freshness, and a content hash. Deduplicate stories, detect conflicting reports, and never let unverified text directly authorize an order.

## Models
Update event-specific probabilities from verified injuries, lineups, live scores, statistics, weather, schedule context, odds movement, and market microstructure. Record forecasts and outcomes to evaluate calibration and incremental value on future unseen events.

## Controls
Respect rate limits and site terms. Use backoff, caching, source health checks, and stale-data gates. Unsupported sports or missing live feeds must be reported as unavailable, not fabricated. Combo mode remains off until actual supported Kalshi contracts are verified. No UI changes or live-mode activation.