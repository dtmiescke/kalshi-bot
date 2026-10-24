# Integration map

Where each planned item belongs in the existing code, so nothing is built twice.

**Source caveat.** The full build plan and the Don Theo spec are not in this
repository, its issues or its PRs. PR #10 calls itself "standalone v1 safety
modules from the master plan", and that is the only trace. The items below come
from that PR, its open question, and gaps found by reading `bot/`, `tests/`,
`index.html` and `.github/workflows/`. When the plan text is added, map each of
its items against the **Owner** column and extend this table. Do not add a
parallel module.

Rules that apply to every row: paper mode only, never commit to `main`, the
existing tests must pass, and there is one owner per capability (see the
ownership matrix in `docs/open-source-integration-audit.md`).

## Single owners (already exist)

| Capability | Owner | Notes |
|---|---|---|
| Crypto contract probability | `bot/live/settlement.py::prob_above` | Averaging window and CF Benchmarks proxy variance |
| Crypto research pipeline (snapshot and live) | `bot/crypto_brain.py::research` | One pipeline for both modes |
| Volatility forecast, tail adjustment | `bot/vol.py` | `tail_shrink` and `learn_tail_shrink` |
| Feature row for the model | `bot/features.py::build` | `model_prob` still uses a fixed 0.9 |
| Signal blend, calibration, weight learning | `bot/brain.py` | |
| Edge, fees, sizing, per-bet caps | `bot/risk.py::decide`, `taker_fee` | The fee math used in production |
| Circuit breakers, arbitrage | `bot/guard.py` | |
| Live 15-minute loop | `bot/live/runner.py` | `tick` → `plan_for` → council → `finish` recheck → order |
| Snapshot cycle, ladders, order placement | `bot/engine.py` | `evaluate` → `place` → `_place_locked` recheck |
| Trained model | `bot/ml.py`, `bot/history.py`, `bot/backtest.py` | |
| Grading, ledger, audit | `bot/grader.py`, `bot/storage.py` | |
| Reports and dashboards | `bot/report.py`, `bot/metrics.py`, `bot/dashboard.py`, `index.html` | No UI changes unless asked |
| DB health and janitors | `bot/nerd.py`, `bot/maintenance.py` | |
| LLM council and briefing | `bot/council.py`, `bot/sports_counsel.py`, `bot/briefing.py`, `bot/llm.py` | |
| Alerts | `bot/alerts.py` | |
| Scheduling and persistence | `.github/workflows/bot.yml`, `train.yml`, `.github/load.sh`, `save.sh` | Workflows force paper mode unless both live approvals are set |

## Plan items

| # | Item | Status | Where it fits |
|---|---|---|---|
| 1 | **tail_shrink fix** (unbounded fat-tail fit) | Done in Task 1 PR | `vol.tail_shrink` / `vol.learn_tail_shrink`: clamp to [0.80, 1.05] and fall back to 0.9. No new module. |
| 1b | **Crypto long-shot guard** (11–17¢ YES bets valued at about 1%, the PR #10 open question) | Done in Task 1 PR | `risk.decide` (`raw_model_prob`, `CRYPTO_LONGSHOT_PRICE` in `config.py`). Wired at `runner.plan_for`, `engine.evaluate` and `engine._place_locked`. Not a second risk engine. |
| 2 | Make the tail fit use the same z as live pricing | Follow-up | `features.py` `model_prob` lacks proxy variance and uses 60-minute volatility, while live pricing uses the blended forecast. Fix inside `ml.learn_tails` / `vol.learn_tail_shrink` by storing the live z in `ml_rows`. Do not fork the formula. |
| 3 | Kalshi fee module (`bot/fees.py`, PR #10) | Exists, not used | Duplicates `risk.taker_fee`. Either make `risk.taker_fee` delegate to `fees.fee` once rates are verified, or delete `fees.py`. Never keep two fee formulas in use. |
| 4 | Silent-failure detector (`bot/health_checks.py`, PR #10) | Exists, not used | Call `health_checks.run_all` from `main.run_scheduled` after the cycle summary. Send paused areas through `guard.status` (durable key, like `execution_reconciliation_required`) and alarms through `alerts.send`. Do not create a new pause store. |
| 5 | Live/archive history router (`bot/kalshi_history.py`, PR #10) | Exists, not used | Use it inside `grader.settle_bets` / `learn_from_markets` and `history.settled_markets` in place of direct `client.market` calls, after the API paths are verified. |
| 6 | Don Theo voice (`bot/don_theo.py`, PR #10) | Exists, not used | Read-only presentation layer. Inputs: `risk.decide` reasons and `engine._record_decision` (decisions), `grader.settle_bets` (settlements), `guard.status` and health alarms (`bad_news`). Output: `briefing.write` and `alerts.send` text, plus an optional line in the `dashboard.py` and `index.html` feed. It must never change a decision. The AI-written variant belongs in `briefing.py`, using the existing `llm.LLM` budget. Spec details are needed before wiring. |
| 7 | Execution and reconciliation safety | Exists | `engine._confirmed_ioc_fill`, the runner `maker_recovery` intent and `guard.status`. Extend these rather than adding a tracker. |
| 8 | Combo orders | Blocked by design | `combo_research.py` is read-only. `COMBOS_ENABLED=yes` fails validation in `config.validate`. |

## Plan text still needed

Paste the full build plan and the Don Theo spec, and each item gets a row above
with one owner. The parts most likely to need them are the Don Theo items
(moods, roasts and their thresholds, where lines appear) and any task numbers
after 1.
