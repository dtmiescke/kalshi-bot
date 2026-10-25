# Sports data and model-quality additions

## Guardrails
This increment is additive. It does not replace the current Odds API or ESPN collector, change the dashboard, alter the existing probability blend, change bet eligibility, or enable new network calls. The existing Engine remains the sole risk/execution authority.

## Model quality metrics
`bot/sports_model_quality.py` adds dependency-free Brier score, binary log loss, calibration bins/error, probability-space closing-line value (CLV), and an evaluation summary. Invalid labels/probabilities are ignored rather than silently coerced. Metrics are diagnostic only; model promotion is always disabled by this module. Compare models on chronological out-of-sample records using the same events, market types, and decision timestamps. Report sample counts and uncertainty; do not use a small win streak as evidence of skill.

CLV requires comparable no-vig entry and closing probabilities for the exact same outcome/market. It is not ROI. If the current data store does not retain a trustworthy closing snapshot, CLV must remain unavailable rather than be inferred.

## Optional open-data research catalog
The source catalog records candidates for future adapters. It does not fetch anything. Existing ESPN and Odds API sources remain canonical until an adapter passes provider terms/license review, sport/season coverage checks, bounded timeout/body/cache/request budgets, stable event/player identity mapping, deduplication, provenance, fail-closed behavior, fixture tests and regression tests, and shadow evaluation.

Prioritize NFLverse for NFL historical play-by-play/EPA; NBA API for NBA player/team stats; pybaseball for MLB Statcast; SportsDataverse as a common access layer where it adds data not already present; OpenLigaDB/football-data/ClubElo for soccer fixtures, historical results and baseline ratings. Do not add all packages to runtime dependencies: they overlap, some rely on fragile public endpoints, and large datasets can create job-time, storage, and bandwidth bottlenecks.

## Integration gate
Before a provider is connected to the live collector, measure which existing fields it fills. Only enable fields missing from the canonical record. Persist one normalized record per provider/event/stat/timestamp key, preserve source identity and timestamps, and never treat missing injuries or lineups as evidence that a player is available.
