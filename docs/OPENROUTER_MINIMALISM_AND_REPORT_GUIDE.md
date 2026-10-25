# OpenRouter token efficiency and readable performance reports

## Concise response policy

The existing `bot/llm.py` adds a short system instruction to ordinary OpenRouter/OmniRoute calls and OpenRouter web-search calls. It asks models to avoid filler and repetition while preserving decision-critical evidence, uncertainty, citations, requested JSON fields, probabilities, votes, and risk rules.

- Controlled by `LLM_MINIMALISM_ENABLED=yes` (default).
- Set `LLM_MINIMALISM_ENABLED=no` to disable the extra system message if a provider/model has a format compatibility issue.
- This is a verbosity policy, not a guarantee of lower total cost: it adds a small system prompt, so actual savings depend on how many output tokens it avoids. Existing request budgets, model fallback, tools, data collection, risk controls, and execution paths are unchanged.
- It does not summarize or truncate raw prompts, odds, player/team evidence, tool results, or JSON inputs. That avoids silently dropping information needed for a decision.
- Validate token usage and response quality against the same test prompts before any future prompt compaction or model-routing changes.

## Performance report: plain English

The report labels Brier score as **prediction error (Brier)** and includes a compact glossary:
- **P&L:** dollars won or lost after fees.
- **ROI:** profit/loss divided by money committed.
- **CLV:** whether the entry price was better than the final market price; useful evidence, not a profit guarantee.
- **Brier / prediction error:** average squared difference between predicted chance and the final result; 0 is perfect, lower is better.
- **Calibration:** whether events assigned a given probability happen about that often.
- **Signal weights:** how strongly the bot currently uses an input; not a stand-alone quality grade.
- **Log loss:** another probability-error measure that penalizes very confident wrong predictions.
- **Promoted model:** passed the existing unseen-data checks; it is not a guarantee of future performance.

The changes are explanatory only. They do not change probability calculations, model promotion criteria, bet eligibility, or execution.
