# Open-source integration audit (initial)

Status: research plan only. This document does not enable or change live trading, dependencies, runtime behavior, or the UI.

## Existing capabilities verified in this repository

- Risk and order planning: `bot/risk.py` (fee calculations, ask reconstruction from Kalshi bid books, depth-aware fill planning, timezone-aware day boundaries, event/window exposure helpers).
- Historical training and model promotion: `bot/ml.py` (time-ordered train/validation/test splits, model comparison against market price/champion, promotion gates and fees).
- Live streaming and rechecks: `bot/live/runner.py` (market feeds, analytics, shortlist/council flow, order-book rechecks).
- Event execution, research, and decision audit: `bot/engine.py`.
- Persistent telemetry and SQLite ledger: `bot/storage.py`.
- Mobile dashboard is a separate existing asset; do not redesign it as part of these integrations.

This overlap means importing full competing trading systems would duplicate infrastructure and introduce unnecessary risk.

## Candidate-by-candidate decision

| Project | License (repository metadata, checked 2026-10-21) | Useful concepts | Decision |
|---|---|---|---|
| Hummingbot | Apache-2.0 | Execution lifecycle, order/fill state handling, order-book patterns | Study relevant implementation and tests. Do not add the full framework or its exchange adapters to the runtime. |
| Freqtrade | GPL-3.0 | Backtest discipline, parameter search, performance reporting | Use as a methodological reference first. Do not copy code or add a dependency without an explicit license/compatibility review. |
| Microsoft Qlib | MIT | Feature evaluation, model experiments, research pipelines | Optional offline research spike only. Do not add to production dependencies unless a benchmark on Kalshi data proves incremental value. |
| NautilusTrader | LGPL-3.0 | Event-driven simulation, ordering and fill lifecycle | Study architecture/tests; avoid wholesale engine replacement. Review LGPL obligations before any code-level reuse. |
| ryanfrigo/kalshi-ai-trading-bot | MIT | Kalshi-specific market ingestion, settlement-grounded evaluation, edge measurement | Highest relevance for comparative audit. Reuse only independently reviewed components that fill a documented gap and pass local tests. |

Licenses and default branches can change; re-check the exact commit and license file before copying code. This table records repository metadata, not legal advice.

## Findings from the next source comparison (2026-10-21)

- **Kalshi-specific reference:** its README describes forward-only settlement evaluation, Brier score/log-loss, and comparison of realized win rate with the implied price paid. This bot already has settlement grading, decision-audit outcomes, Brier scores, model-vs-Kalshi log-loss, and chronological splits. Therefore, do not import a duplicate evaluator; use those concepts to check for metric/reporting gaps.
- **Freqtrade:** its backtesting test suite is extensive and emphasizes explicit, testable behavior around fills, fees, and exit conditions. The appropriate adaptation is targeted offline regression tests in this repository, not the Freqtrade runtime or GPL code.
- **Qlib:** keep experiments optional and offline. A framework import is not evidence of better forecasts; compare against the current model on the same chronological holdout and retain only if it improves proper scoring and net-of-cost simulated results.
- **Hummingbot / NautilusTrader:** use their order-book and order-lifecycle testing patterns as design references. No upstream implementation code has been copied into this bot in this step; upstream test paths and APIs must be pinned to a reviewed commit before any further reuse.

### Local correction made during this pass

The model trade simulator now rounds a one-contract fee up to the same cent boundary as risk.taker_fee, and the ROI denominator includes both premium and fee. The latter matters because excluding fees from committed capital inflates reported returns. Regression coverage checks the exact expected fee and net ROI. CI status for the latest commit must be verified before calling this step complete.

## Duplicate-feature / ownership matrix

| Capability | Single source of truth in this bot | Do not add in parallel |
|---|---|---|
| Contract fees and cost basis | `bot/risk.py` (`taker_fee`) | Separate fee formulas inside model, replay, or external adapters |
| Historical replay and realized outcomes | `bot/history.py`, `bot/backtest.py` | Independent ROI formulas in each replay path |
| Model fitting, time splits, promotion gate | `bot/ml.py` | Qlib/Freqtrade runtime model trainer in production |
| Live order book, freshness, and final recheck | `bot/live/` runner/book modules | Hummingbot/NautilusTrader order-book or execution engine replacement; pending maker orders block competing candidates until reconciled |
| Settled-bet grading, CLV, decision audit | `bot/grader.py` + `bot/storage.py`; ROI reporting via `bot/report.py` and `bot/metrics.py` | Duplicate settlement/grading tables or independent ROI formulas |
| Risk limits and order sizing | `bot/risk.py`, `bot/guard.py`, `bot/engine.py`, `bot/storage.py` | Second independent risk engine; daily, event, and crypto-window spend caps now include fees |
| User interface | Existing dashboard assets | External project dashboard or UI framework replacement |

**Implementation from this pass:** `bot/ml.py` calls `bot.risk.taker_fee` rather than maintaining a separate fee-rounding formula. `bot/ml.py`, `bot/history.py`, and `bot/backtest.py` use `bot/metrics.py::binary_contract_roi`. The realized performance report uses `bot/metrics.py::net_roi` and now includes fees in committed capital; reported P&L remains net of fees. The shared helper handles zero capital safely. A regression test covers the shared helper. This is consolidation, not another feature. No upstream source code or production dependency was added.

## Recommended integration order

1. Finish and review the current safety/memory PR; do not merge automatically.
2. Add or improve offline execution simulation tests in the existing test framework: partial fills, stale books, fees, latency assumptions, unfilled orders, settlement and residual exposure.
3. Benchmark the existing model and calibration against market-implied probability using strictly chronological, out-of-sample settled data. Report sample size and uncertainty; do not treat a profitable backtest as proof of future returns.
4. Compare Kalshi-specific edge/settlement evaluation methods with existing decision-audit and grading code; implement only a missing capability.
5. Run an isolated Qlib research experiment only if the current dataset and sample count support a fair comparison. Keep it outside the production dependency path until it demonstrates reproducible incremental value.
6. Borrow targeted execution test cases and design ideas from Hummingbot/NautilusTrader, rather than importing their entire engines.
7. Publish a change manifest, test results, dependency/license review, and rollback notes in a pull request. Keep the existing dashboard untouched.

## Acceptance gates for any future implementation

- Existing regression suite passes.
- New tests cover failure paths and risk limits.
- No stale or missing data is silently treated as valid evidence.
- No model is promoted without a genuinely untouched chronological holdout and adequate sample count.
- All simulated P&L includes fees and conservative fill assumptions.
- No duplicate orders or bypass of existing risk gates.
- No new paid service or required paid API.
- No UI/dashboard changes unless explicitly requested.
- No merge to `main`, or live-trading activation, without user approval.

## Scope and limitations

This is an initial architecture/source review, not a formal security audit of every file in each upstream project. No upstream code has been copied by this document. The goal is to select and verify small, justified improvements instead of combining entire bots.


## Expanded pinned-source comparison (2026-10-22)

This is a targeted source review, not a line-by-line audit of each entire upstream repository. The following upstream source files were retrieved and inspected directly; refs are pinned so the reviewed snapshot can be reproduced.

| Upstream | Reviewed source at pinned commit | What it contributes to the comparison | Decision for this bot |
|---|---|---|---|
| Hummingbot | [client_order_tracker.py](https://github.com/hummingbot/hummingbot/blob/9af100d6822da7d2d0291a906c730ef172284ee2/hummingbot/connector/client_order_tracker.py) — `9af100d6822da7d2d0291a906c730ef172284ee2` | Client-side order state tracking and exchange event reconciliation | Keep the existing Kalshi runner/client. Continue adding focused tests around missing status, partial fills, cancellation and reconciliation; do not introduce a second order tracker without a demonstrated gap. |
| Freqtrade | [backtesting.py](https://github.com/freqtrade/freqtrade/blob/0db79d391f646eafbdf3c0976dd55dc90b296522/freqtrade/optimize/backtesting.py) — `0db79d391f646eafbdf3c0976dd55dc90b296522` | Backtest execution assumptions, order processing and performance accounting | Use as a test-design reference. Do not copy GPL-3.0 implementation code into the product without a deliberate distribution/licensing decision. |
| Microsoft Qlib | [backtest.py](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/backtest/backtest.py) — `54355232463878d2eebb91fe0ee5fa7fa1f5976c` | Separation of strategy decisions, execution and portfolio/account simulation | Keep any experiment offline and benchmarked on the same chronological settled Kalshi windows. Qlib is not currently a production dependency. |
| NautilusTrader | [manager.rs](https://github.com/nautechsystems/nautilus_trader/blob/bb8050e2173851ec84ce78b553cd5089fe232166/crates/execution/src/order_manager/manager.rs) — `bb8050e2173851ec84ce78b553cd5089fe232166` | Explicit order lifecycle and execution state management | Retain the current smaller runner. Use lifecycle cases to guide tests; do not wholesale replace the engine or copy LGPL-3.0 code without license review. |
| Kalshi AI Trading Bot | [jev_backtest.py](https://github.com/ryanfrigo/kalshi-ai-trading-bot/blob/7b9667bb7e7e7b263f14b54ab8647a05cbdd1383/scripts/jev_backtest.py) — `7b9667bb7e7e7b263f14b54ab8647a05cbdd1383` | Explicit out-of-sample evaluation against Kalshi market prices at a defined decision time | Existing bot already has chronological splits, market-implied log-loss comparison and settlement grading. Compare methodology and add only a missing, tested metric—not a duplicate evaluator. |

The pinned snapshots were resolved through GitHub repository metadata and source files were retrieved. The license identifiers were separately checked from repository metadata on 2026-10-21/22: Hummingbot Apache-2.0; Freqtrade GPL-3.0; Qlib MIT; NautilusTrader LGPL-3.0; Kalshi AI Trading Bot MIT. This is an engineering comparison, not a legal opinion or a guarantee that every file in a repository has identical licensing.

### Commercial ownership constraint

The intended product is proprietary software owned by the user, with the option to offer subscription access later. No upstream source code was copied in this comparison and no runtime dependency was added. Prefer independent implementations of general ideas. Before any future code reuse or dependency addition, record the exact source commit, file, license, modifications, notices/source-offer obligations (if any), dependency transitivity, and whether distribution or hosted use changes the analysis. The Freqtrade GPL-3.0 and NautilusTrader LGPL-3.0 projects need particular review before code-level reuse. Subscription hosting does not transfer ownership of the user's original code, but it also does not erase third-party license obligations.

### Verification checkpoint

- Latest checked CI for branch head `e6f7d2a0d3899d5e1009a5ab76fcd5bc347fec9b`: 136 tests passed; Python compilation and workflow shell validation passed.
- PR #2 is still open and unmerged.
- The pending-maker regression confirms a second candidate is blocked while an earlier maker order is tracked. This is a conservative interim guard, not a complete reservation ledger; the robust future design is explicit reserved exposure plus reconciliation of unknown order states.
- Remaining verification is broader than CI: this review has not established live exchange correctness under every network failure, conducted an external security audit, verified every deployment secret/setting, or proven trading profitability. Those are not claims this document makes.


## Additional model-artifact recovery fix (2026-10-22)

The champion promotion path now writes the candidate bundle to a temporary file, verifies that the serialized artifact can be loaded, and atomically replaces the active model file. The previous model and metadata are copied to timestamped backups before the active artifact changes. New bundles include their own metadata, so prediction uses a consistent model/metadata pair even if the human-readable JSON sidecar update is interrupted. Legacy model bundles still fall back to the existing JSON metadata for compatibility. Unreadable/corrupt model artifacts fail closed to no prediction rather than being trusted.

Regression tests cover successful artifact promotion and a simulated interruption between model replacement and sidecar replacement. This improves recovery safety but does not eliminate every filesystem, storage-provider, or power-loss failure mode. The latest workflow must pass on the final branch head before this change is considered verified.


### Maker order identity safeguard

A real/demo maker response without an exchange order ID is no longer inserted into the in-memory maker tracker as though it were safely managed. The runner records a skipped reason and pauses further betting pending reconciliation. A regression test covers the missing-ID response. This is a conservative stop, not automatic recovery: if the exchange accepted an order but omitted its ID, the operator still needs to reconcile the account's open orders before resuming.


### Maker fill-count ambiguity

Order reconciliation now treats missing or malformed fill counts as unknown rather than proof of zero fills. Explicit terminal `executed`/`filled` states without a count conservatively reserve the requested quantity as filled; explicit canceled/expired/rejected states without a count are treated as zero filled. Ambiguous nonterminal states remain tracked. Regression tests cover these cases. This policy intentionally favors not losing a possible position over silently releasing exposure.
