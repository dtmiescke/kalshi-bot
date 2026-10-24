# kalshi-bot - FULL BUILD PLAN

Everything except Phase 6 (Don Theo / Council), which is in docs/DON_THEO_SPEC.md.
App: dtmiescke.github.io/kalshi-bot - Bot runs every 12 min via GitHub Actions - PAPER mode

## Ground rules (every task)
- One branch + one PR per task. Never commit to main. Owner reviews before merge.
- Paper mode only. Live stays LOCKED: needs real keys + LIVE_TRADING_CONFIRMED=yes + 30+ graded bets showing real edge.
- Zero cost: free tiers only, no credit card anywhere. Bot slows down + alerts at 80% of any limit.
- Read Kalshi docs (docs.kalshi.com/llms.txt) before touching any Kalshi code.
- Tests for every change; all existing tests must pass.
- Leave the bot-data branch alone.
- Reuse what exists (see docs/INTEGRATION_MAP.md). Never build a second copy of an existing capability.

## Order of work
1. Fix tail_shrink bug (most important)
2. Wire in the PR #10 safety modules (fees.py, health_checks.py, kalshi_history.py, don_theo.py) - keep ONE fee implementation
3. Delete stale/merged branches
4. Phase 1 Foundation (janitor trim BEFORE the reliable timer)
5. Phases 2, 3, 4, 5, 6 (Don Theo spec), 7, 8
God Mode and Kalshi-proofing are built alongside, starting in Phase 1.

## Task 1 - tail_shrink + crypto long-shot guard
Problem: vol.learn_tail_shrink could fit values down to 0.5; at 0.5 a ~1% long shot prices at ~10-13%, so the bot bought 11-17c sides. Calibration: 127 predictions near 5% -> 0 hits.
Decisions:
- Tail value clamped 0.80 to 1.05; default 0.9. Bad/missing stored value -> 0.9.
- Only accept a newly fitted value if it beats 0.9 on NEWER held-out windows (split by window, >= 50 holdout windows); otherwise keep 0.9. Save fit details for the app.
- Fit against the SAME price calculation the bot bets with (blended vol forecast + settlement averaging), not the simpler features.model_prob.
- learn_tails uses only rows of the current FEATURE_VERSION.
- Long-shot guard: crypto side under CRYPTO_LONGSHOT_PRICE (default 0.20) only if the raw crypto model alone shows the edge. Live planner, snapshot runs, final pre-order check.
- Log the tail value used with every crypto prediction.

## Task 2 - PR #10 modules
- fees.py: verify against Kalshi's fee schedule; must agree with risk.taker_fee; keep one implementation.
- kalshi_history.py: verify every path against Kalshi docs; grader uses it (live first, then archive) so settled bets never go ungraded.
- health_checks.py: run_all(stats) at end of every run; pause only affected areas on critical alarms. Merge with nerd.py rather than duplicating.
- don_theo.py: used by Phase 6.

## Task 3 - branch cleanup
Delete merged/stale branches. Never delete bot-data or any corrupt-backup branch without owner approval.

## Known problems to fix along the way
1 GitHub scheduler skips runs -> Cloudflare cron timer synced to Kalshi's 15-min clock
2 Database large and growing -> janitor trim first
3 Bot and trainer can overwrite each other's saves -> shared lock + Turso
4 Trainer has no time limit -> add one
5 Old order book format reads -> every price read uses orderbook_fp first
7 ESPN NASCAR/cricket/UFC feeds fail -> remove
8 Odds quota tight -> smart scheduling
9 NBA stats blocked on cloud -> ESPN + official NBA injury report (nba_api later from PC)
11 App loads the full DB -> Worker serves small summaries
12 Council labels overlap -> declutter
13 Bot memory is public -> private Turso
14 Kalshi archives settled data -> kalshi_history fallback

## Phase 1 - Foundation
- Honest paper results: fees in every edge/profit, realistic fills, closing line value.
- Librarian/janitors: hot data ~48h, warm ~90d, trade records forever; casebook; backup before any cleanup.
- Cloudflare cron timer (after trim); trainer time limit; bot/trainer collision guard.
- Alarms, heartbeat watchdog (nerd), tests before every merge.
- Storage: Turso (SQLite, free, private) + Cloudflare Worker (summaries + AI proxy) + R2 archive.

## Phase 2 - Pro data layer
- Official sources: MLB Stats API + Statcast (pybaseball), NHL API (Zmalski/NHL-API-Reference), nflverse + NFL injury report, ESPN + NBA injury report, CollegeFootballData (free key), MoneyPuck.
- Cross-checks, freshness stamps, sanity rules, a data confidence grade per game.
- Odds every 4 hours, smart: only leagues with games, more near game time, hard stop at free limit.
- Player profiles: 3-year lookback, recency weighted, impact-when-missing. Team profiles: 5-year, roster-turnover discount.
- Officials, coaches, venues, weather, head-to-head.
- Weekly Top Performers by position with trend arrows (Data Floor).
- Matchup simulation (thousands of sims, consistent prices for every market incl. combos). Fast news reaction.

## Phase 3 - Brain + self-learning
- Confidence range on every prediction; bet only when the worst end beats price after fees.
- Context weighting: meta-model learns when to trust each signal by sport/bet type/data quality.
- Live updates on news; drawdown-limited sizing; half-Kelly default.
- Correlation guard, liquidity filter, informed-money detection, value-of-information budget.
- Why card for every bet AND every pass.
- Self-learning: weights update after grading; ML retrains (300+ windows); Council members scored; post-mortems; lessons library (tested, owner approves); casebook; strategy tournament; drift detection; calibration layer fixing long-shot bias.

## Phase 4 - Advanced crypto engine
- Kalshi WebSocket CF Benchmarks: cfbenchmarks_value (1/sec), cfbenchmarks_value_5hz (200ms; BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI, DOGEUSD_RTI). Needs Kalshi API key.
- BRTI replica from constituent exchange books (backup + next-tick predictor).
- 60-second settlement averaging (live/settlement.py), Monte Carlo, jumps, clustered vol, regimes, Deribit IV, calendar effects, order-flow pressure.
- Ladder consistency, cross-timeframe checks, final-minute overreaction edge, long-shot correction, after-fee edges only.
- Fill probability, adverse-selection guard, cancel-and-replace, latency check, kill switch.
- Discover every Kalshi crypto coin automatically; read each market's rules text.

## Phase 5 - Sports engine
- Full scan: moneylines, spreads, totals, player props.
- New-market detector; favorite tilt (long shots overpriced).
- Combo engine: combo only when EV beats separate legs.
- Props from own projections; sportsbooks only for best candidates.
- Aggressive paper strategy as a tournament entry. Pre-game only, fast re-checks on news.

## Phase 6 - Council + Don Theo -> docs/DON_THEO_SPEC.md

## Phase 7 - App (same 4 tabs: HQ, Council, Trades, Control)
- Declutter: spacing, one main number per card, collapsible sections, consistent colors, plain labels, no overlapping labels at 380px.
- Council tab: Chamber (ground floor) + elevator -> Data Floor with one bot per source, health lights, flow lines: MLB, NHL, NFL, NBA, College, News, Odds, Kalshi, Crypto, Librarian, PLUS the 2 Janitor bots and the Nerd Watchdog (back ends: maintenance.py, nerd.py). Each bot looks like what it's called: janitors with mop/bucket/broom, nerd with glasses/clipboard/pocket protector. Show each one's latest status/action.
- Live scores per open bet; bet story timeline; plain-English report; Top Performers board.
- Control: MIN_BET, aggression dial (Cautious -> Aggressive), big pause button, Data Health card.
- AI chat via OpenRouter through the Worker; ntfy notifications; pull-to-refresh; search/filter; haptics; changelog.
- Speed: small summary JSON, instant load from cache then refresh, tabs load on tap.
- Show amounts in dollars/percentages, not cents.

## Phase 8 - Demo, PC, prep
- Kalshi demo (demo.kalshi.co) to test real order placement with play money. Real Kalshi key (free) unlocks WebSocket + CF Benchmarks.
- PC hybrid: hidden background service, auto-start on Windows, low priority; continuous crypto runner; Ollama for the Council; nba_api from home IP.

Kalshi WebSocket channels: cfbenchmarks_value, cfbenchmarks_value_5hz, ticker, trade, orderbook_delta, market_lifecycle_v2, multivariate, multivariate_market_lifecycle, fill, market_positions.
Prod: wss://external-api-ws.kalshi.com/trade-api/ws/v2 - Demo: wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2

## God Mode (study loop)
Cloud ~50 AI requests/day batched; PC full speed with Ollama. Studies every settled bet, skipped market, game result, crypto window, news reaction speed. Produces casebook entries, tested patterns (must hold on unseen data and beat fees), proposed lessons (owner approves). Feeds Librarian, source trust scores, post-mortems, Council grades, Sunday Sit-Down.
Upgrades: code does math/AI explains; proper pattern testing (no leakage, multiple-comparison correction); what-if replays; second-opinion reviewer; smart study queue; lesson scorecard (undo if not helping); owner thumbs up/down; monthly playbooks with knowledge decay.

## Kalshi-proofing
1 One gateway module for every Kalshi call
2 Validate responses against Kalshi's OpenAPI spec; flag unknown/missing fields
3 Daily spec/changelog diff with alert
4 Fallbacks: orderbook_fp -> bid/ask -> last trade; live -> archive -> trades
5 Canary test every run before betting
6 Daily contract tests vs live and demo
7 Fail safe: pause only the affected market type, reason shown in app
8 Auto GitHub issue on failure + phone ping

## Study list (ideas; respect licenses)
TauricResearch/TradingAgents, Polymarket/agents (MIT), agent-next/polymarket-paper-trader, kyleskom/NBA-Machine-Learning-Sports-Betting (ideas only), gmalbert/nfl-predictions (ELITE/STRONG/GOOD/LEAN tiers), ryanfrigo/kalshi-ai-trading-bot, SportsDataverse, Zmalski/NHL-API-Reference + nhl-sdk, MoneyPuck, pybaseball, nba_api, greerreNFL/nfeloqb, nfelo, CF Benchmarks BRTI methodology, Kalshi API docs
