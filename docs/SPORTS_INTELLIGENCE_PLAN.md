# Free-first Sports Intelligence Upgrade

This is an implementation note for the existing Kalshi Intelligence Bot. It must preserve the existing UI and keep live trading disabled during validation.

## Implemented on the staging branch
- A scheduled, cached sports-intelligence collector using public Google News RSS and ESPN scoreboard/summary endpoints.
- Sport-level news queries plus a bounded number of event-specific queries per scan.
- Scoreboard states/scores and bounded live/final box-score snapshots, where ESPN exposes them.
- Source health, timestamps, provenance, duplicate detection, size limits, timeouts, and failure isolation.
- A read-only command: `python -m bot.main sports-intel`; use `--force` to bypass source cache.
- Dynamic discovery of Kalshi series in the Sports category and bounded discovery of open events.
- Manual series allowlist through environment variables; no dashboard or navigation changes.
- A gated, read-only discovery pass for actual Kalshi multivariate/combo events.

## Sports selection
- `SPORTS_MODE=all` (default): discover the public Kalshi Sports series catalog and open events.
- `SPORTS_MODE=manual`: only scan the comma-separated series tickers in `SPORTS_SELECTED`.
- Existing configured odds-backed series are NFL, NBA, MLB, NHL and college football.
- Other discovered sports are scanned and recorded, but remain research-only unless the bot has a verified independent probability source and a supported odds mapping. Do not assume that catalog discovery alone qualifies a series for automated trading.

Example manual allowlist:
`SPORTS_MODE=manual`
`SPORTS_SELECTED=KXNFLGAME,KXNBAGAME,KXMLBGAME`

## Free source inventory
- Google News RSS: broad sports news discovery and a small cached event-specific query budget.
- ESPN public scoreboard/summary endpoints: scores, game state, selected box-score/team/player statistics where available.
- Existing Kalshi public market data: series catalog, open events, nested markets, prices and trade data.
- Existing The Odds API integration: only within its configured free quota and available sports/markets.
- Existing bot features: sportsbook consensus, odds history, market order book, flow, settlement, model history and graded outcomes.

Unofficial public endpoints may change or throttle requests. Each feed is best-effort; a failed source is recorded as failed, not treated as a negative signal. News and score data are labeled unverified until corroborated by an authoritative source. The collector respects timeouts and source caches; it does not bypass access controls, paywalls, logins or rate limits.

## Research-to-decision controls
- Event/entity matching is required before evidence is passed into the existing team-specific AI review.
- Headlines are explicitly treated as untrusted external claims, not instructions.
- Old headlines are discarded from AI review; source and observation timestamps are preserved.
- New sports without supported independent odds/model coverage are research-only and cannot bypass existing pre-game matching and risk gates.
- Collected data is stored in the existing SQLite database and does not require a new paid database or service.

## Combos
`COMBOS_ENABLED=no` by default. If enabled, the bot discovers and records actual open Kalshi multivariate events. This is **research-only**: the current client does not yet implement the complete combo RFQ/quote, joint-probability, fees, fill/reconciliation and settlement lifecycle, so it does not place combo orders. Do not set this to yes expecting combo execution.

## CI and validation
The staging branch adds a free GitHub Actions workflow that compiles Python modules and runs the unittest suite on branch pushes and pull requests. Do not merge until CI passes and the data/API behavior is reviewed.

## Known limits
- This is not a guarantee of comprehensive or second-by-second live coverage. The bot's scheduled GitHub Actions job runs in periodic bursts.
- Some ESPN endpoints may not support every sport or may change. The collector reports source failures.
- Dynamic discovery is bounded (up to 10 pages of series and 20 pages of events); a full page limit means coverage may be incomplete.
- Free feeds do not provide complete Vegas odds, injury data, player stats and live updates for every sport.
- No UI files are changed by this upgrade scope.
