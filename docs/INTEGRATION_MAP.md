# Integration map

Where each planned item belongs in the existing code, so nothing is built twice.
One owner per capability (see also the ownership matrix in
`docs/open-source-integration-audit.md`). Paper mode only, never commit to
`main`, and the existing tests must pass.

**Plan text still missing.** `docs/BUILD_PLAN.md` and `docs/DON_THEO_SPEC.md`
are meant to hold the full build plan and the Don Theo spec verbatim. Their
text has not reached this session yet. Until it does, the item list below
comes from PR #10, the owner's instructions and the code. Every plan item
must get a row here before it is built.

## 0. Before building: persistent memory (bot-data)

Checked 2026-10-24 against `origin/bot-data` (`7e66eeb`, saved 19:13 UTC) and
`origin/bot-data-corrupt-backup-20261024` (`c87e054`, saved 2026-10-23 19:24 UTC).

| Check | bot-data (current) | corrupt backup |
|---|---|---|
| gzip stream | ok | ok |
| SQLite `PRAGMA integrity_check` | **ok** | **fails**: rowid out of order in tree 15; rows missing from `sqlite_autoindex_kv_1` |
| Tables `load.sh` and the Nerd require | all present | all present |
| Rows (bets / decision_audit / graded_markets / kv) | 5 / 8,354 / 3,369 / 23,771 | 5 / 7,560 / 3,069 / 19,856 |
| Nerd health in DB | `nerd_last_health.ok = true`, quick_check ok | n/a |

**Save/load path.** `load.sh` restores `bot.sqlite.gz`, refuses to start fresh
when the branch exists but has no database, and runs `quick_check` before
allowing a save. `save.sh` snapshots with SQLite's online backup API, runs
`integrity_check` and force-pushes. The backup is corrupt because run 28 was
cancelled mid-save at 19:24 on 10-23. That happened before the online-backup
fix (#12). Runs 29–34 then failed closed, which is the intended behaviour.
The latest run (36, 19:00 on 10-24) saved successfully.

**Blockers found:**

1. **The database file will soon exceed GitHub's 100 MB limit.**
   `bot.sqlite.gz` is 61.7 MB (233 MB uncompressed) and was 50.9 MB a day
   earlier. `flow_trades` holds about 980k rows and, with its two indexes,
   takes up about 180 MB of the 233 MB. Its 7-day retention window has not
   started trimming yet, because the oldest row is from 10-20. At this rate the
   push fails within days and memory stops saving.
   Owner: `maintenance.janitor_retention` / `Store.compact`, by shortening
   `flow_trades` retention (only the last 5–15 minutes are ever read). Also
   `kv`: 17k `start_*` / `settle_after_*` keys are never trimmed. Fix both
   before any feature work.
2. **Scheduled runs are on an old commit.** The last successful scheduled run
   used `19761e5` (#25), not the current `main`. GitHub fires the 12-minute
   cron only every few hours. Check this before trusting any paper results
   from the new code.
3. **The corrupt backup branch** can be deleted once you agree. It is not read
   by anything.

## 1. Single owners (already exist)

| Capability | Owner |
|---|---|
| Crypto contract probability | `bot/live/settlement.py::prob_above` |
| Crypto research pipeline (snapshot and live) | `bot/crypto_brain.py::research` |
| Volatility forecast and tail adjustment | `bot/vol.py` |
| Model feature row | `bot/features.py::build` |
| Signal blend, calibration, weight learning | `bot/brain.py` |
| Edge, fees, sizing, per-bet caps | `bot/risk.py::decide`, `taker_fee` |
| Circuit breakers, arbitrage | `bot/guard.py` |
| Live 15-minute loop | `bot/live/runner.py` (`tick` → `plan_for` → council → `finish` recheck) |
| Snapshot cycle, ladders, order placement | `bot/engine.py` (`evaluate` → `place` → `_place_locked` recheck) |
| Trained model, replay, backtest | `bot/ml.py`, `bot/history.py`, `bot/backtest.py` |
| Grading, ledger, decision audit | `bot/grader.py`, `bot/storage.py` |
| Reports, dashboard, phone UI | `bot/report.py`, `bot/metrics.py`, `bot/dashboard.py`, `index.html` |
| Data janitors | `bot/maintenance.py` |
| Nerd watchdog | `bot/nerd.py` (+ `main.run_scheduled`, `nerd-check` step in `bot.yml`) |
| LLM council, counsel, briefing | `bot/council.py`, `bot/sports_counsel.py`, `bot/briefing.py`, `bot/llm.py` |
| Alerts | `bot/alerts.py` |
| Scheduling and persistence | `.github/workflows/*.yml`, `.github/load.sh`, `.github/save.sh` |

## 2. Plan items

| # | Item | Status | Where it fits |
|---|---|---|---|
| T1a | **tail_shrink fix** | Built in a first version, needs revising to the new spec | `vol.tail_shrink` / `vol.learn_tail_shrink`. **Spec:** limits 0.8–1.2, default 0.9. Fit against the z that `prob_above` actually uses. Accept a new value only if it beats 0.9 on newer held-out windows (at least 50). See the Task 1 details below. |
| T1b | **Crypto long-shot guard** | Built in a first version, needs revising to the new spec | `risk.decide`, plus `CRYPTO_MIN_PRICE` (default 0.20) in `config.py`, wired at `runner.plan_for`, `engine.evaluate` and `engine._place_locked`. **Spec:** a hard price floor plus the "raw model must also support it" check. |
| D1 | **Data Floor: Janitor A (dedupe)** | Exists, UI only | Logic: `maintenance.janitor_dedupe`. UI data: kv `janitors_last_result.janitor_a`, `janitors_last_run`. |
| D2 | **Data Floor: Janitor B (retention)** | Exists, UI only. Blocker 0.1 is its job | Logic: `maintenance.janitor_retention`. UI data: kv `janitors_last_result.janitor_b` (before/after row counts). |
| D3 | **Data Floor: Nerd watchdog** | Exists, UI only | Logic: `nerd.inspect_code`, `nerd.inspect_store`, `nerd.record_success`. UI data: kv `nerd_last_health`, `nerd_last_healthy_ts`, `nerd_last_full_check_ts`. |
| D4 | Data Floor room in the UI | New, UI work | Add to `ROOMS` / `AGENTS` / `roomInfo` in `index.html`. It already loads `bot.sqlite.gz` in the browser (FLAWS #35), so it reads the kv keys above directly. Add no new backend. |
| F1 | Kalshi fee module (`bot/fees.py`, PR #10) | Exists, not used | Duplicates `risk.taker_fee`. Make one delegate to the other after rates are verified. Never keep two fee formulas in use. |
| F2 | Silent-failure detector (`bot/health_checks.py`, PR #10) | Exists, not used | Call `health_checks.run_all` from `main.run_scheduled`. Pauses go through `guard.status` (durable kv key), alarms through `alerts.send`. A natural extra Data Floor agent. |
| F3 | Live/archive history router (`bot/kalshi_history.py`, PR #10) | Exists, not used | Use it inside `grader.settle_bets` / `learn_from_markets` and `history.settled_markets` after the API paths are verified. |
| DT | Don Theo (`bot/don_theo.py`, PR #10) | Line bank exists, not used | Presentation only, and it must never change a decision. Inputs: `risk.decide` reasons and `engine._record_decision`, `grader.settle_bets`, `guard.status` and health alarms (`bad_news`). Outputs: `briefing.write`, `alerts.send`, and a feed line in `index.html`. AI-written lines go in `briefing.py` on the existing `llm.LLM` budget. Detailed rows wait for `DON_THEO_SPEC.md`. |
| X1 | Execution and reconciliation safety | Exists | `engine._confirmed_ioc_fill`, the runner `maker_recovery` intent, `guard.status`. Extend these and do not add a second tracker. |
| X2 | Combo orders | Blocked by design | `combo_research.py` is read-only, and `COMBOS_ENABLED=yes` fails validation. |

## 3. Task 1 details and a correction to the diagnosis

The live database shows that the cheap bets were **not** caused by a learned
tail value:

- No `tail_shrink_*` key has ever been stored, so pricing has always used the
  0.9 default. `ml_rows` has only 204 rows, below the 200-per-coin fit
  threshold.
- The three 11–17¢ YES bets (10-22, BTC/ETH 15-minute) had **raw**
  `crypto_model` values of 23–29%. Using the bot's own inputs (BTC 0.066%
  under the strike, 8.4 minutes left, 0.037%/minute volatility, z ≈ −0.6),
  about 29% is the correct output of the formula. The "~1% fair value" from
  PR #10 can't be reproduced from what the bot saw.
- So the raw-model check would **not** have blocked them. Only the hard 0.20
  floor would have. Their outcome: −$2.52, +$5.68 (sold early), −$2.34.

What this means:

- The tail fix is still worth doing as a latent bug, because an unbounded fit
  could reach 0.5 once enough rows are labelled. But it is not what bought
  these contracts.
- The real open question is why Kalshi priced them at half the model. The
  likely candidate is the gap between our spot price (Coinbase/Kraken) and CF
  Benchmarks near the strike. `SETTLE_PROXY_BPS=3` may be far too small, or
  the spot feed may lag. Owner: `settlement.prob_above` proxy variance (FLAWS
  #9), measured from graded windows in `grader.py`. Proposed as T1c,
  investigation first.

Revised Task 1 build (waiting for approval):

1. `vol.py`: limits [0.8, 1.2], default 0.9, and an invalid stored value falls
   back to 0.9.
2. `ml_rows` stores the z that `prob_above` used (with its actual sigma,
   averaging window and proxy variance) at log time, in `crypto_brain.research`.
   `learn_tail_shrink` fits on that z. Rows without it are skipped.
3. Acceptance: fit on older windows, evaluate on the newest windows (at least
   50 whole windows, grouped like `ml.time_split`). Store the new value only if
   its log-loss beats 0.9 on that holdout. Record the result in kv for the UI
   and the report.
4. `risk.decide`: crypto sides below `CRYPTO_MIN_PRICE` (0.20) are refused
   outright (the hard floor), and the raw `crypto_model` must also show the
   edge by itself. Rejection reasons name the guard in the decision audit.
5. Tests for each item. All existing tests must pass.

To confirm: is the floor strict (refuse below $0.20), or does it include
$0.20? One profitable NO bet was bought at exactly 20¢. The raw-model check
would have passed both NO bets (20¢ and 23¢), since the raw model gave them
33% and 37%.
