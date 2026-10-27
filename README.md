# Kalshi Intelligence Bot

Research-first, self-learning trading bot for Kalshi **sports** and **15-minute crypto**
markets. It bets only when its estimate beats the price **after fees**, measures itself
honestly, and keeps learning. Everything runs on free services.

Win rate is not the goal: buying $0.85 favourites wins 85% of the time and still loses money.
The tests that matter are **closing-line value** (sports) and **beating Kalshi's own price
on unseen data** (crypto).

## Schedule (GitHub Actions, free on a public repo)
| Every | What |
|---|---|
| 12 min | 8-minute **live streaming burst** on BTC/ETH/SOL/BNB 15-minute markets, grading, learning, AI briefing |
| 12 min | eight read-only specialists scan all events in the five **currently configured sports**; a bounded rotating subset gets deep research, and open positions are always revisited |
| daily | **retrain** the model on all history + live decisions; promote only if it wins on unseen data |
| weekly | download fresh real Kalshi history |
| daily | trim raw data so the memory file stays small (learning is never trimmed) |

## Continuous 15-minute crypto runner (persistent host)

The GitHub Actions workflow remains the short-cycle backup; GitHub-hosted jobs are
not a 24/7 process and cannot guarantee exact start times. For continuous streaming,
run the existing bot in a persistent container. The service prepares its feeds and
market discovery first, then aligns feed startup to the next UTC quarter-hour boundary.
It keeps one-second analytics running across BTC, ETH, SOL, and BNB 15-minute Kalshi
markets, refreshing market discovery every minute and restarting after transient errors.

1. On a persistent Docker host, clone this repository and create a local `.env` file.
2. Put any required API keys in `.env` (never commit it). Public crypto spot feeds do
   not need exchange API keys; Kalshi trading/data credentials and OpenRouter are optional
   according to which existing features you use.
3. Start with `docker compose up -d --build`; check logs with
   `docker compose logs -f kalshi-crypto`.
4. The container deliberately forces `BOT_MODE=paper` and
   `LIVE_TRADING_CONFIRMED=no`. Do not remove these safeguards as part of deployment.
5. The container keeps its SQLite database under the host's `./data` directory. That
   database is local to this service and is **not automatically synchronized** with the
   GitHub Actions memory snapshot/dashboard. Do not enable real-money execution or treat
   this as the authoritative dashboard until shared persistence/ownership is deliberately
   designed and verified.

The continuous process is intended for an always-on VM/container. It is not run inside
the GitHub Actions job because GitHub-hosted jobs have a maximum duration and would stop.
Kalshi's listed 15-minute contracts define the actual windows; the bot continuously
monitors their live books and signals rather than placing a forced bet at each boundary.

## Execution safety
- Scheduled runs default to paper mode. Setting `BOT_MODE=live` alone is not enough to enable real-money orders.
- The scheduled workflow also requires the repository variable `LIVE_TRADING_OPERATIONAL_APPROVED=yes` and `LIVE_TRADING_CONFIRMED=yes` before it will use live mode.
- Keep the approval variable unset until restored memory, market discovery, price freshness, risk checks, persistence, and repeated paper cycles have all passed. Paper mode uses real public market prices but does not send real orders.

## Reliability agents (existing project, no UI replacement)
- **Janitor A — deduplication:** deduplicates raw market, sportsbook-odds, and signal observations by natural key, then adds SQLite unique indexes so later cycles cannot insert the same observation twice. The append-only bet ledger, model weights, lessons, grades, and decision audit are never deduplicated or deleted.
- **Janitor B — retention:** trims only old raw snapshots using the existing retention windows; the normal daily compaction remains responsible for VACUUM.
- **Nerd watchdog:** checks the required database structure and SQLite health before a scheduled cycle, runs a full integrity check daily, records health, and saves an incident when a scheduled run fails. The GitHub workflow also compiles bot modules before execution and checks the database afterward.
- **Safe recovery policy:** automatic repair is limited to deterministic data cleanup and bounded runtime checks. If SQLite integrity fails, the bot fails closed and reports the incident instead of rewriting source code or risking learned history. Source-code fixes must pass tests and a reviewed PR; self-modifying production code could create a larger failure.
- **Sports selection is preserved:** `SPORTS_MODE=all` discovers supported open Kalshi sports series; `SPORTS_MODE=manual` uses `SPORTS_SELECTED`. Add more sports through the existing series/catalog configuration. These reliability agents do not replace the engine, sports list, dashboard, or trading strategy.

## Sports selection
- Default `SPORTS_MODE=configured` keeps the bot on its five existing leagues: NFL, college football, NBA, MLB, and NHL.
- Set `SPORTS_MODE=manual` and `SPORTS_SELECTED=...` to choose any specific Kalshi sports series, including additional leagues you intentionally add.
- Set `SPORTS_MODE=all` only when you explicitly want the bot to discover every sports series in Kalshi's catalog. No new leagues are silently enabled by default.

- The fleet's fast coverage scan runs over the full eligible event set. Expensive per-market history and research is capped by `SPORTS_SCAN_EVENT_BUDGET` (default 150 events) and `SPORTS_SCAN_MARKET_BUDGET` (default 100 markets) so the scheduled job can finish and persist memory. Increase budgets only after observing runtime; held events bypass the deep-market cap.

## Crypto microstructure and chart diagnostics
- The existing BTC/ETH/SOL/BNB 15-minute Kalshi workflow is extended in place; sports discovery, event research, risk gates, execution, storage, and the existing dashboard are not replaced.
- A public Coinbase Level-2 order book is sampled through the existing market-data context and cached for 5 seconds. It records spread, top-10-level bid/ask depth imbalance, and microprice displacement. This is explicitly a **cross-venue proxy**, not Kalshi liquidity. The bot also uses its existing KalshiClient to sample the actual Kalshi YES/NO bid-depth book, cache it per ticker for 5 seconds, and emit a separate low-confidence depth reading. Neither book is a standalone trade trigger.
- The existing stable OHLCV feature pipeline already calculates multi-timeframe returns, RSI, EMA trend, MACD histogram, ADX, Bollinger bands, ATR, VWAP distance, OBV slope, volume spikes, and volatility regime. Those features remain unchanged so the training/live feature schema does not silently drift. A compact indicator snapshot is exposed alongside the existing crypto research info.
- Crypto research info includes a TradingView chart link for the matching Coinbase spot pair. This is a chart hand-off, not a claim that TradingView supplies an official free market-data API to this bot.
- Order-book failures are non-fatal and fail closed; stale or missing depth never becomes a fabricated signal. No API key is required for this public Coinbase endpoint. The existing OpenRouter key/configuration is reused only for the existing AI research features; no additional secret is required for the order book.
- These are research features, not a profit guarantee. Cross-venue depth can be spoofed or differ from Kalshi's settlement index. They must be judged in time-split/out-of-sample tests before any weight increase. Paper mode and existing execution/risk controls remain authoritative.

## OpenRouter research brain
- OmniRoute remains the preferred general-purpose gateway when configured; OpenRouter remains the fallback. The existing LLM budget and model panel are reused rather than adding another agent stack.
- When `OPENROUTER_API_KEY` is configured, the existing sports news analysis can add bounded OpenRouter web search for event-specific news and official updates. Evidence is cached per event (30 minutes by default), with source URLs and a research-health record.
- The scheduled job also runs a weekly public-source technology scout. It can search and fetch public GitHub/docs/paper pages on approved domains, then stores up to five candidate engineering ideas in `openrouter_tech_scout_latest`. Ideas are recommendations only; the scout cannot edit code, execute downloaded code, alter model weights, or enable trading.
- Web search is enabled by default when an OpenRouter key is present. Default limits are 2 search requests per scheduled run and 6 per UTC day; actual charges depend on OpenRouter's selected search engine and model. Disable with `SPORTS_AI_WEB_SEARCH=no`, `OPENROUTER_TECH_SCOUT_ENABLED=no`, or set the search caps to `0`.
- Useful repository variables: `OPENROUTER_WEB_SEARCH_MODEL`, `OPENROUTER_WEB_SEARCH_MAX_PER_RUN`, `OPENROUTER_WEB_SEARCH_DAILY_LIMIT`, `SPORTS_AI_WEB_SEARCH_TTL_SECONDS`.
- Search results and fetched pages are untrusted evidence. The bot requires provenance and freshness, and research cannot override its single engine, paper-mode defaults, execution guards, or risk limits.

## Smart research
**Crypto (`crypto_brain.py`, used by snapshot AND live modes):**
- Exact contract model: strike, seconds left, settlement averaging window from the rules
- **Volatility forecaster** (`vol.py`): EWMA + 60-min realized + Deribit option-implied vol,
  time-of-day profile learned from history, bumps around CPI/Fed/jobs releases
- **Every trading tool** (`features.py`): RSI (1/5/15m), Stochastic RSI, MACD, EMA trends,
  ADX, Bollinger %B and width, ATR, VWAP distance, OBV, volume spikes, day high/low,
  round-number levels, multi-timeframe momentum — all scaled to volatility units
- **Market context** (`marketdata.py`): futures funding, open interest change, basis,
  options put/call ratio, Coinbase-vs-Kraken premium, Fear & Greed, economic calendar
- **Live streams** (`live/`): Kraken 100-level book + trades, Coinbase ticker, Kalshi book;
  depth, imbalance, aggressive flow, walls appearing / pulled / consumed, data-quality checks.
- **Crowd / money-flow intelligence**: sportsbook odds are de-vigged and tracked over time;
  sports Kalshi public prints are measured for YES/NO aggressor imbalance, large prints and sweeps.
  The crypto live runner now also polls Kalshi's anonymous public trade tape and blends its
  recent flow signal with Kraken/Coinbase aggressor flow. These feeds show side and size,
  not individual bettor identities or account-level positions.

**Hourly/daily crypto ladders** ("BTC above $X at 5 PM"): traded directionally with the same
exact-contract model every run — stricter (8% edge, $5 bets, one strike per event) since the
trained model only knows 15-minute windows — plus cross-market ladder arbitrage.

**Sports:** sharp-weighted vig-free consensus, line movement and steam, reverse line
movement (hook), Kalshi trade flow, rest / back-to-back / road-trip spots, AI news panel.

## Self-learning
1. **Trained model (`ml.py`)** — gradient-boosted trees on every tool above, stacked with the
   exact-contract formula and Kalshi's price. Strict time-ordered train / validate / test
   split; each 15-minute window counts once. **Promoted only if** it beats Kalshi's price
   AND the current champion on unseen windows AND its simulated trades profit after fees.
   Old champions are kept. Reports which tools actually matter.
2. **Every live decision becomes training data**, labelled automatically at settlement.
3. **Signal weights** re-learned per domain (sports vs crypto) on every settled market.
4. **Volatility blend** re-learned from realized moves.
5. **Self-calibration**: when it says 70%, does that happen ~70%? It corrects itself.
6. **Backtests**: `history` (real Kalshi + Coinbase replay) and `backtest` (recorded research).

## Smart execution
- **Maker orders**: rests a limit order one tick inside the spread (cheaper, lower fee);
  cancels if unfilled after 45 s. Never leaves orders behind.
- **Early exits**: sells when the bid is worth more than holding (`EXIT_MARGIN`).
- **Arbitrage**: two-team sports arb (skips leagues that can tie) and **cross-market crypto
  ladder arb** (YES lower strike + NO higher strike < $1, same settlement rules only).
- **Council** (`council.py`): free LLMs review shortlisted crypto trades (analyst, liquidity,
  probability, risk, chair + independent skeptic). Can reject or wait, never enlarge.
- **Recheck** with the freshest book right before every order.
- **Account limits**: virtual cash can't go negative, max open positions (5), max total open
  cost ($200), daily realized-loss breaker ($30, resets at midnight UTC).
- **Kraken checksum**: every book update is fingerprint-checked; a mismatch reconnects for a
  fresh snapshot instead of trading on a corrupted book.
- **Risk**: fractional Kelly or `FIXED_BET`, caps per bet / game / day / **15-minute window**
  (BTC+ETH+SOL move together), drawdown and negative-CLV circuit breakers, league benching.
- **Earned sizing**: bets start at 25% of normal size and grow only with a profitable track record.
- **Learned order style**: if resting orders rarely fill (<25%), it switches to taking the ask.
- **Phone alerts** (`alerts.py`): bets, sales, arbitrage, pauses, new models — via free ntfy.

## AI gateway routing: OmniRoute, LiteLLM, and OpenRouter
Every AI feature (news panel, council, briefings) keeps using the existing OpenAI-compatible
client and shared call budgets. The route order is **OmniRoute → LiteLLM Proxy → OpenRouter**:
- `OMNIROUTE_URL`, `OMNIROUTE_API_KEY`, `OMNIROUTE_MODELS`: existing preferred gateway.
- `LITELLM_URL`, `LITELLM_API_KEY`, `LITELLM_MODELS`: optional LiteLLM Proxy fallback; model
  names must be aliases actually configured in that proxy.
- `OPENROUTER_API_KEY`, `OPENROUTER_MODELS`: existing fallback and OpenRouter-only web search.
No second LLM SDK is required for the proxy path; both gateways use the existing HTTP client.
All calls still share `LLM_DAILY_LIMIT` / `LLM_MAX_CALLS_PER_RUN` and the concise-output policy.
If a gateway is unreachable, it is skipped for the rest of that run. `python -m bot.main check`
can verify AI connectivity without placing trades. LiteLLM is optional; leave its settings blank
to preserve the existing OmniRoute → OpenRouter behavior.

## Integration choices from the open-source review

Detailed reference-by-reference reverse-engineering decisions and acceptance criteria: [docs/OPEN_SOURCE_INTEGRATION_REVIEW.md](docs/OPEN_SOURCE_INTEGRATION_REVIEW.md).
- **Contract-rule review:** `bot/market_rules.py` normalizes native and common read-only MCP rule fields and shares a compact audit with sports counsel and crypto settlement metadata. When native market terms are missing, the existing Kalshi client may retrieve a bounded text extract from an allowlisted official contract PDF. PDF text alone cannot satisfy the settlement-source gate; missing or ambiguous terms remain review-required. No second exchange client or order-write MCP is added.
- **Keep one execution authority.** Do not connect a third-party MCP trading server with order-write
  access alongside `KalshiClient`; that would duplicate order paths and bypass the current final
  risk recheck, ledger, and reconciliation guard. A future Kalshi MCP adapter should be read-only
  for contract rules, market metadata, and research until it can be proven to use the same guarded
  execution path.
- **Do not copy another bot's sizing or market-making loop blindly.** Existing fee-aware fractional
  Kelly, liquidity-aware fill planning, caps, and circuit breakers remain authoritative. Half-Kelly
  or Avellaneda–Stoikov market making requires independent out-of-sample validation and is not
  enabled by this integration.
- **Keep data sources matched to enabled markets.** The current bot focuses on sports and crypto.
  NWS/GFS weather feeds and FRED macro series are candidates only if weather/macro Kalshi markets
  are added with explicit settlement-source and freshness rules; they are not injected into sports
  probabilities. The existing ESPN, sportsbook, Kalshi, and crypto feeds stay in place.
- **FIX is not switched on by assumption.** It needs official account/API eligibility, protocol
  support, order-state reconciliation, and benchmarked latency/failure tests. REST remains the
  existing execution path until that work is separately validated.

## Mobile dashboard (free, GitHub Pages)
After each run the bot writes `index.html` + `bets.csv` to the `bot-data` branch. Turn on
Settings → Pages → Branch: `bot-data` and open `https://<you>.github.io/<repo>/` on your
phone: status, Kalshi connection health, cash, open positions with quote age, live feed
health, recent results, model status, AI briefing, and a CSV of every trade. Public, like
the repo.

## Kalshi connection safety
Honors Kalshi's Retry-After, one shared cooldown for the whole bot, ~8 requests/second
pacing, brief caching of market LISTS only (prices are never cached), and signed reads with
your data key. No host or proxy rotation to dodge limits. If Kalshi keeps refusing,
`python -m bot.main diagnose` writes `DIAGNOSE.txt` with the evidence for Kalshi support.

## Modes
`BOT_MODE=paper` (default, real prices, simulated) → `demo` (Kalshi play money) →
`live` (refused unless `LIVE_TRADING_CONFIRMED=yes`).

## Commands
```
python -m bot.main check        # verify keys + connectivity
python -m bot.main scheduled    # what GitHub runs every 12 minutes
python -m bot.main history 14   # real settled 15-min windows: replay + training data
python -m bot.main train        # train/test/promote the model
python -m bot.main live 30      # stream for 30 minutes
python -m bot.main backtest     # replay recorded research
python -m bot.main report | brief | compact
python -m unittest discover tests
```

## Free tier
| Piece | Cost |
|---|---|
| Kalshi data & history, Kraken, Coinbase, Deribit, ESPN, alternative.me, Forex Factory feed | free, no key |
| Kalshi API key (demo trading, WebSocket) | free account |
| The Odds API | free key (bot budgets its 500 credits/month) |
| OpenRouter free models | free (~50/day; ~1000/day after a one-time $10 credit) |
| ntfy.sh alerts | free |
| GitHub Actions | free unlimited on a **public** repo |

## Profit-leak audit
`FLAWS.md` lists 23 ways the bot could lose money or fool itself (look-ahead in
backtests, stale prices, fee rounding, adverse selection, half-filled arbitrage,
model decay, ...) with the fix and a regression test for each (`tests/test_audit.py`).

## Verify before trusting
Kalshi series tickers (`config.py`, `CRYPTO_LADDER_SERIES`), sports fee rate, settlement
averaging window (`SETTLE_AVG_SECONDS` if rules don't state it). ESPN and Forex Factory feeds
are unofficial. Nothing here guarantees profit: paper results over hundreds of bets, and a
promoted model, are the evidence.
