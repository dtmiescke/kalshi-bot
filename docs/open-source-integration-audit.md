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
