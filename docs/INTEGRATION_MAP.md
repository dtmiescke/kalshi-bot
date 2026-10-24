# Integration map

Where every item in `docs/BUILD_PLAN.md` and `docs/DON_THEO_SPEC.md` belongs
in the existing code. Rule: **one owner per capability**. A new item extends
its owner and never adds a parallel copy. Paper mode only, one branch and one
PR per task, never commit to `main`, and the existing tests must pass.

Status key: **Exists** = already built, reuse it. **Partial** = an owner
exists and needs extending. **New** = no owner yet, so this row names where it
goes. **Blocked** = needs something outside the repo (an account or key).

## 0. Before building: persistent memory (bot-data)

Checked 2026-10-24 against `origin/bot-data` (`7e66eeb`, 19:13 UTC) and
`origin/bot-data-corrupt-backup-20261024` (`c87e054`, 2026-10-23 19:24 UTC).

| Check | bot-data (current) | corrupt backup |
|---|---|---|
| gzip stream | ok | ok |
| SQLite `PRAGMA integrity_check` | **ok** | **fails**: rowid out of order in tree 15; rows missing from `sqlite_autoindex_kv_1` |
| Required tables | all present | all present |
| Rows (bets / decision_audit / graded_markets / kv) | 5 / 8,354 / 3,369 / 23,771 | 5 / 7,560 / 3,069 / 19,856 |
| Nerd health stored in DB | `nerd_last_health.ok = true` | n/a |

**Save/load path.** `load.sh` restores `bot.sqlite.gz`, refuses to start fresh
when the branch exists without a database, and runs `quick_check` before
allowing a save. `save.sh` uses SQLite's online backup API, runs
`integrity_check` and force-pushes. The backup is corrupt because run 28 was
cancelled mid-save at 19:24 on 10-23, before the online-backup fix (#12).
Runs 29–34 then failed closed, as intended. Run 36 (19:00 on 10-24) saved
successfully. Following the plan, the bot-data branches are left alone.

**Blockers, in the order you chose:**
1. **Storage:** `bot.sqlite.gz` is 61.7 MB (233 MB raw), up from 50.9 MB a day
   earlier. `flow_trades` and its indexes take about 180 MB of that, and kv has
   17k `start_*` / `settle_after_*` keys that are never trimmed. It will hit
   GitHub's 100 MB file limit within days. Plan known problem 2. Owner:
   `maintenance.janitor_retention` / `Store.compact`.
2. **Scheduled runs:** no `bot.yml` run fired between 19:00 and 21:22 UTC on
   10-24 despite the 12-minute cron, so the code merged at 19:51–20:56 never
   ran on a schedule. Plan known problem 1.

## 1. Single owners (already exist)

| Capability | Owner |
|---|---|
| Every Kalshi HTTP call | `bot/kalshi_client.py` (`KalshiClient._req`) |
| Crypto contract probability | `bot/live/settlement.py::prob_above` |
| Crypto research pipeline | `bot/crypto_brain.py::research` |
| Volatility forecast, tail adjustment | `bot/vol.py` |
| Model features | `bot/features.py::build` |
| Signal blend, calibration, weight learning | `bot/brain.py` |
| Edge, fees, sizing, caps | `bot/risk.py::decide`, `taker_fee` |
| Circuit breakers, arbitrage | `bot/guard.py` |
| Live 15-minute loop | `bot/live/runner.py` |
| Snapshot cycle, ladders, order placement | `bot/engine.py` |
| Trained model, replay, backtest | `bot/ml.py`, `bot/history.py`, `bot/backtest.py` |
| Grading, ledger, decision audit (why-card data) | `bot/grader.py`, `bot/storage.py` |
| Sports scanners, intel, counsel | `bot/sports_fleet.py`, `bot/sports_intel.py`, `bot/sports_counsel.py`, `bot/signals/*` |
| Data janitors | `bot/maintenance.py` |
| Nerd watchdog | `bot/nerd.py` (+ `main.run_scheduled`, `nerd-check` step in `bot.yml`) |
| LLM access with daily cap | `bot/llm.py` |
| Crypto council | `bot/council.py` |
| Briefing | `bot/briefing.py` |
| Alerts (ntfy) | `bot/alerts.py` |
| Reports, dashboard | `bot/report.py`, `bot/metrics.py`, `bot/dashboard.py` |
| Phone app (HQ / Council / Trades / Control) | `index.html` |
| Schedules and persistence | `.github/workflows/*.yml`, `.github/load.sh`, `.github/save.sh` |

## 2. Order of work (BUILD_PLAN)

| # | Item | Status | Where it fits |
|---|---|---|---|
| P0a | Storage trim (known problem 2) | Partial | `maintenance.janitor_retention` + `Store.compact`: shorter `flow_trades` retention, trim kv keys of long-settled tickers. Runs before the timer change (plan: "janitor trim BEFORE the reliable timer"). |
| P0b | Scheduled runs reliable (known problem 1) | Partial | Now: `bot.yml` (workflow-level fix). Later: Cloudflare cron timer synced to Kalshi's 15-minute clock (Phase 1). That one is **Blocked** on a Cloudflare account. |
| T1 | tail_shrink + crypto long-shot guard | Partial (first version on `claude/task1-tail-shrink-longshot-guard`) | See section 5. Owners: `vol.py`, `ml.learn_tails`, `crypto_brain.research`, `risk.decide`, `config.py`. |
| T1c | Spot vs CF Benchmarks price-gap check | New | `grader.py` (measure on graded windows) → `settlement.prob_above` `proxy_bps`. See section 5. |
| T2a | `fees.py` | Exists, not used | Verify against Kalshi's fee schedule, then make `risk.taker_fee` the one implementation, either delegating to `fees.fee` or with `fees.py` removed. `ml.sim_trades` and `metrics` already call `risk.taker_fee`. |
| T2b | `kalshi_history.py` | Exists, not used | `grader.settle_bets`, `learn_from_markets` and `grade_decision_audits` call `HistoryRouter.market` (live first, then archive). Also covers known problem 14. |
| T2c | `health_checks.py` | Exists, not used | `run_all(stats)` at the end of `main.run_scheduled`, **merged into `nerd.py`'s report** (`nerd_last_health`) and not a second watchdog. Critical alarms set a per-area pause read by `guard.status` / `guard.league_benched`. |
| T2d | `don_theo.py` | Exists | Phase 6 (section 4). |
| T3 | Branch cleanup | New, ops only | List merged branches and delete them after approval. Never delete `bot-data` or `bot-data-corrupt-backup-20261024`. |

## 3. Known problems → owner

| # | Problem | Owner |
|---|---|---|
| 1 | Scheduler skips runs | P0b. Cloudflare cron later (Blocked: account) |
| 2 | Database large and growing | P0a |
| 3 | Bot and trainer overwrite each other's saves | Partial: `bot.yml` and `train.yml` already share `concurrency.group: kalshi-bot`, so they don't run at the same time. But `save.sh` force-pushes, so a lost or stale restore would still overwrite. Add a base-commit check in `save.sh`. Turso later (Blocked: account) |
| 4 | Trainer has no time limit | Partial: `train.yml` has a 90-minute job timeout, which kills the job without saving. Add a deadline inside `ml.train` so it stops cleanly and saves |
| 5 | Old order-book format | Partial: `kalshi_client.orderbook` already reads `orderbook_fp` first and `price()` prefers `*_fp` fields. Remaining: the WebSocket book (`live/kalshi_book.py`, `feeds.handle_kalshi`) and any direct price reads must use the same helpers |
| 7 | ESPN NASCAR/cricket/UFC feeds fail | `sports_intel.py` source list (remove them) |
| 8 | Odds quota tight | `odds.odds_due` + `engine.refresh_odds` (only leagues with games, more near game time, hard stop at the free limit) |
| 9 | NBA stats blocked from cloud runners | `sports_intel.py`: ESPN + official NBA injury report. `nba_api` later from the PC (Phase 8) |
| 11 | App loads the full DB | `dashboard.py` writes a small `summary.json` to bot-data, and `index.html` reads it. Worker later (Blocked: account) |
| 12 | Council labels overlap | `index.html` Council view (Phase 7) |
| 13 | Bot memory is public | Turso (Blocked: account). Until then bot-data stays on the public branch |
| 14 | Kalshi archives settled data | T2b |

## 4. Phases

### Phase 1: Foundation
| Item | Status | Owner |
|---|---|---|
| Fees in every edge and profit | Exists | `risk.decide` (real-order fee re-check), `metrics.net_roi`, `ml.sim_trades` |
| Realistic fills | Exists | `risk.fill_plan*`, `runner.check_makers` (trade-through fills), `history.evaluate` slippage |
| Closing-line value | Exists | `grader.closing_yes`, `guard.clv_tripped` |
| Librarian / janitors: hot ~48h, warm ~90d, trades forever, backup before cleanup | Partial | `maintenance.py` (hot tier = P0a). Warm tier and casebook: new tables in `storage.py`, owned by the janitors. Backup before cleanup: `save.sh` snapshot. |
| Cloudflare cron, trainer limit, collision guard | Blocked / Partial | Known problems 1, 3, 4 |
| Alarms, heartbeat watchdog | Exists / Partial | `nerd.py` + `alerts.py`, merged with T2c |
| Turso + Worker + R2 | Blocked | Needs free Cloudflare/Turso accounts. Then `storage.Store` gets a Turso backend behind the same interface. |

### Phase 2: Pro data layer
| Item | Status | Owner |
|---|---|---|
| Official sources (MLB Stats/Statcast, NHL API, nflverse, NBA injury report, CFBD, MoneyPuck) | New | One fetcher per source inside `sports_intel.py` (same cache, health and provenance), with its readings added through `signals/` |
| Cross-checks, freshness stamps, sanity rules, data-confidence grade | Partial | `sports_intel.health` + per-source status. The grade feeds `Reading.confidence` |
| Smart odds every 4h | Partial | Known problem 8 |
| Player / team profiles, officials, venues, weather, head-to-head | Partial | `sports_intel._football_matchup_profile` is the pattern. Extend it per sport, never as a second collector |
| Weekly Top Performers | New | Computed in `report.py`, shown on the Data Floor |
| Matchup simulation (consistent prices, combos) | New | A new `bot/sim.py` producing a `Reading` (`signals/` contract). `combo_research.score_combo_probabilities` uses its joint probabilities |
| Fast news reaction | Partial | `sports_intel.collect_event_news` + `engine.news_reading` |

### Phase 3: Brain and self-learning
| Item | Status | Owner |
|---|---|---|
| Confidence range; bet only if the worst end beats price after fees | New | `brain.blend` returns an interval, and `risk.decide` tests its lower end |
| Context weighting meta-model | Partial | `brain.get_weights` (already per league) gains a learned context layer in `brain.py` |
| Half-Kelly, drawdown-limited sizing | Partial | `risk.decide` (`KELLY_FRACTION`), `guard.drawdown_tripped` |
| Correlation guard, liquidity filter | Partial | `risk.staked_on_window` / `has_open_event_bet` (correlation), `risk.fill_plan` depth (liquidity) |
| Informed-money detection | Partial | `signals/flow.py` |
| Value-of-information budget | New | `llm.LLM` budget + `engine` research gating |
| Why card for every bet and pass | Partial | `decision_audit` rows already hold reasons and signals. Render them in `index.html` |
| Self-learning, retrain, post-mortems, lessons, casebook, tournament, drift | Partial | `brain.learn`, `ml.train`, `grader.monitor_model` (drift). Lessons and casebook use the existing `lessons` table. Tournament: `backtest.py` variants |
| Calibration fixing long-shot bias | Partial | `brain.calibrate` (+ T1 guard) |

### Phase 4: Advanced crypto engine
| Item | Status | Owner |
|---|---|---|
| Kalshi WS `cfbenchmarks_value(_5hz)` | New | `live/feeds.py` (subscription + handler) → `live/analytics.SymbolAnalytics.add_reference`. Needs the real data key (`KALSHI_DATA_*`). This is what T1c measures the need for |
| BRTI replica from constituent books | New | `live/analytics.composite_price` |
| 60s averaging, Monte Carlo, jumps, regimes, Deribit IV, calendar | Partial | `settlement.prob_above`, `vol.py`, `signals/implied_vol.py`, `marketdata.calendar` |
| Ladder consistency, cross-timeframe, long-shot correction | Partial | `guard.ladder_arbs`, `engine.run_ladders`, T1 guard |
| Fill probability, adverse selection, cancel-replace, latency, kill switch | Partial | `runner.check_makers` / `_maker_edge_gone`, `storage.maker_fill_rate`, `guard.status` |
| Discover every crypto coin; read rules text | Partial | `runner.discover`, `settlement.contract_terms` |

### Phase 5: Sports engine
| Item | Status | Owner |
|---|---|---|
| Full scan (moneylines, spreads, totals, props) | Partial | `sports_fleet.py` + `engine.run` |
| New-market detector | New | `sports_fleet.normalize_events` diffed against the previous scan (kv) |
| Favorite tilt | Partial | `brain.calibrate` |
| Combo engine | Partial | `combo_research.py`. Orders stay blocked (`COMBOS_ENABLED`) |
| Props from own projections | New | Phase 2 sim → `Reading` |
| Aggressive paper strategy as a tournament entry | New | `backtest.py` / a paper-only `Settings` profile |

### Phase 6: Council + Don Theo (DON_THEO_SPEC)
| Item | Status | Owner |
|---|---|---|
| Line bank, moods, streaks, roasts, bad-news-first | Exists | `bot/don_theo.py` (extend it, e.g. the `hot_hand` line "Somebody get me a bigger chair.", the spec's sample lines, Sunday lines) |
| Five members + Quant, advisory first | Partial | `bot/council.py` (crypto roles today) and `bot/sports_counsel.py` (sports), unified on one member roster in `council.py`. Advisory = verdict recorded, `risk.decide` result unchanged (test) |
| Triage then debate, daily AI cap, rule-based fallback reasons | Partial | `council.review` + `llm.LLM` budget |
| Votes, reasons and ruling saved with the bet/pass | Partial | `decision_audit` (`signals_json`) and bets `signals.council` |
| Member grading (right/wrong + Brier) | New | `grader.py` after settlement, in a new `council_votes` table in `storage.py` |
| Seating, doghouse, earned power (Advisory → Veto → Full) | New | Derived from member grades in `council.py`, with thresholds in `config.py` |
| Memory callbacks from the casebook | New | Phase 1 casebook → `don_theo` |
| AI-written lines with fallback, never blank | Partial | `briefing.py` / `llm.py` → `don_theo` fallback |
| Sunday Sit-Down | New | `briefing.py` weekly, in Don Theo's voice, bad news first |
| Chamber, elevator, Data Floor, animations, voice toggle | New | `index.html` Council tab (Phase 7) |

### Phase 7: App (`index.html`; same 4 tabs)
| Item | Status | Owner |
|---|---|---|
| Declutter, 380px, dollars not cents | Partial | `index.html` |
| **Data Floor bots** | New UI, existing back ends | One bot per source, with data from `sports_intel_health`, `sports_scanner_health`, `kalshi_health`, `live_discovery_health` and `odds`. Plus **Janitor A** (`maintenance.janitor_dedupe`; kv `janitors_last_result.janitor_a`), **Janitor B** (`maintenance.janitor_retention`; kv `janitors_last_result.janitor_b`) and the **Nerd Watchdog** (`nerd.inspect_*`; kv `nerd_last_health`, `nerd_last_healthy_ts`, `nerd_last_full_check_ts`). Each bot is drawn as its name (mop, bucket and broom for the janitors; glasses, clipboard and pocket protector for the Nerd) |
| Live scores per bet, bet timeline, Top Performers | New | `index.html` from `sports_intel` + `bets` |
| Control: MIN_BET, aggression dial, pause, Data Health | Partial | `index.html` sets repo variables (it already does for some). Pause = `guard` kv key |
| AI chat, ntfy, pull-to-refresh, search, haptics, changelog | New / Partial | `index.html`; `alerts.py` for ntfy. Chat via Worker (Blocked: account) |
| Small summary JSON, cache-first load | New | `dashboard.py` writes the summary (known problem 11) |

### Phase 8: Demo, PC, prep
| Item | Status | Owner |
|---|---|---|
| Kalshi demo orders | Exists | `BOT_MODE=demo` + `config.base_url` |
| PC service, continuous crypto runner, Ollama council, nba_api | New | `live/runner.run` as a service, `llm.endpoints_from_env` (OmniRoute/Ollama). Outside CI |

### God Mode and Kalshi-proofing
| Item | Status | Owner |
|---|---|---|
| Study loop, casebook, tested patterns, lessons, Sunday Sit-Down feed | New | `briefing.py` / `grader.py` producers, writing to the Phase 1 casebook and `lessons` tables. Pattern tests reuse `ml.time_split` + `ml.paired_bootstrap` (no leakage) |
| One gateway for every Kalshi call | Exists | `kalshi_client.py` |
| Response validation, spec diff, contract tests | New | `kalshi_client._req` validation hook + a daily `health_checks` check |
| Fallbacks (orderbook_fp → bid/ask → last; live → archive → trades) | Partial | `kalshi_client.orderbook`, T2b |
| Canary before betting; pause only the affected market type | Partial | `nerd.py` + `guard.status` per-area pause (T2c) |
| Auto GitHub issue + phone ping | Partial | `alerts.py`; issue creation in the workflow failure step |

## 5. Task 1 and T1c

**Diagnosis, corrected against the live database:**
- No `tail_shrink_*` key has ever been stored, so pricing always used the 0.9
  default.
- The three 11–17¢ YES bets on 10-22 had **raw** `crypto_model` values of
  23–29%. On the bot's own inputs (BTC 0.066% under the strike, 8.4 minutes
  left, 0.037%/minute volatility, z ≈ −0.6), about 29% is the correct output
  of the formula. Kalshi priced them at about half that.
- So the raw-model check alone would **not** have blocked these bets. The
  hard floor below $0.20 does. The tail fix still closes a real latent bug,
  because the fit was unbounded.

**Task 1, to spec** (values from `BUILD_PLAN.md`):
1. `vol.py`: clamp to [0.80, 1.05], default 0.9, and an invalid stored value
   falls back to 0.9.
2. `crypto_brain.research` logs the tail value used and the live z (its sigma,
   averaging window and proxy variance) in the `ml_rows` feature row, with
   `FEATURE_VERSION`. `ml.learn_tails` uses only current-version rows and fits
   on that z.
3. Acceptance: split by window in time order. A newly fitted value is stored
   only if it beats 0.9 by log-loss on the newer holdout (at least 50
   windows); otherwise 0.9 is kept. Fit details are saved to kv for the app.
4. `risk.decide`: a crypto side priced **below** `CRYPTO_LONGSHOT_PRICE`
   ($0.20; exactly $0.20 is allowed) is refused unless the raw `crypto_model`
   alone shows the edge. This applies in the live planner, snapshot runs and
   the final pre-order check.

**T1c price-gap check:** for graded 15-minute windows, compare our spot price
at the close (from the saved `crypto_model` readings) against Kalshi's
reported settlement value, if the market data exposes one. Report the gap
distribution in bps per coin, and recommend a `SETTLE_PROXY_BPS` value. The
check is read-only and changes nothing automatically.
