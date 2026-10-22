# Profit-leak audit

Every way this bot could lose money or fool itself, what it would have done, and
the fix. Each fix has a regression test in `tests/test_audit.py` (or the test named),
so it can't silently come back.

## A. Fooling itself (results look better than reality)

| # | Leak | Effect | Fix |
|---|---|---|---|
| 1 | **Look-ahead in history replay.** Kalshi candles are stamped at the END of a minute, Coinbase candles at the START. Replay was reading the spot price one minute in the future. | Backtests and the trained model would show an edge that can't exist live. The single most dangerous flaw. | Replay now uses only the candle that closed at or before each Kalshi price. `history.py` · test_01 |
| 2 | **Crypto signals graded on the final second.** The brain judged signals on their last reading before the bell, when everything already knows the answer. | Learned weights reflect nothing about real trading decisions. | Crypto is judged at the last reading ≥2 min before close (end of the trading window). `brain.decision_cutoff` · test_03 |
| 3 | **Promoting a lucky model.** One good test set can happen by chance. | A model with no real skill goes live and bleeds. | Paired bootstrap over test windows: must beat Kalshi in ≥90% of resamples, with ≥100 unseen windows and ≥30 profitable simulated trades. `ml.paired_bootstrap` · test_10 |
| 4 | **Perfect fills in simulations.** Backtests bought at the quoted ask with unlimited size. | Overstated returns. | Half-cent slippage per contract; minutes with spreads over 8c skipped. `ml.sim_trades`, `history.evaluate` · test_11 |
| 5 | **Optimistic maker fills in paper mode.** Counted as filled when the price merely touched the order. | Resting orders sit behind others in the queue; paper P&L overstated. | Paper maker orders fill only when sellers trade THROUGH the price. `runner.check_makers` · test_maker_rests_then_fills |
| 6 | **Train/serve skew.** Model trained on Coinbase candles but live mode built candles from Kraken (different volume scale). | Volume features mean different things live than in training: wrong predictions. | Live mode streams Coinbase trades and builds identical candles. `live/analytics.minute_candles` |

## B. Fake edges (trading on wrong numbers)

| # | Leak | Effect | Fix |
|---|---|---|---|
| 7 | **Stale spot in snapshot runs.** Used the latest candle close, up to a minute old, against a live Kalshi book. | Kalshi's price looks "wrong" only because our price is old. | Snapshot runs use Coinbase's last-trade ticker and skip if older than 15 s. `crypto.fetch_spot` |
| 8 | **Stale sportsbook lines.** Sports scans every 5.5 h could reuse odds up to 6 h old. | Live Kalshi vs old lines = fake edges. | Odds refreshed every scan if older than 60 min; no bet if consensus is older than 90 min. `engine.refresh_odds/evaluate` |
| 9 | **Settlement index ≠ our price.** Kalshi settles on CF Benchmarks; we use Coinbase/Kraken. | Near the strike, the model called coin flips sure things. | The index gap is added as variance (`SETTLE_PROXY_BPS`). `settlement.prob_above` · test_02 |
| 10 | **Fixed fat-tail guess.** The 0.9 tail adjustment was assumed. | Systematic mis-pricing of big moves. | Fitted per coin from real settled windows. `vol.learn_tail_shrink` |
| 11 | **Missed WebSocket updates.** A sequence gap left a wrong Kalshi book in use. | Trading on a ghost book. | Gap marks the book stale (no trading) and triggers a fresh snapshot. `kalshi_book.delta` |
| 12 | **One broken exchange feed.** No check that Kraken and Coinbase agree. | A frozen or glitched feed drives trades. | Data marked stale if they differ by more than 25 bps. `analytics.summary` · test_08 |
| 13 | **Mixed calibration.** Sports and crypto results corrected each other. | One domain's overconfidence warps the other's probabilities. | Calibration per domain. `brain.calibrate` · test_09 |

## C. Execution leaks (paying too much, or getting picked off)

| # | Leak | Effect | Fix |
|---|---|---|---|
| 14 | **Fee rounding on small orders.** Edge was checked with a 100-contract fee estimate; real fees round up per order. | A "5%" edge could really be 3%. | Final edge re-checked on the actual order's fees. `risk.decide` · test_04 |
| 15 | **Kelly ignored fees.** Sized as if the price were the full cost. | Slight systematic oversizing. | Kelly uses the fee-inclusive cost. `risk.decide` |
| 16 | **Adverse selection on maker orders.** A resting order whose edge has faded only gets filled by people who know it's now a bad price. | Fills concentrate on the worst trades. | Cancel a resting order as soon as its edge drops below half the threshold; record edge at fill to measure it. `runner._maker_edge_gone` · test_unfilled_maker_expires |
| 17 | **Churn after early exits.** After selling, the bot could re-buy the same window. | Pays spread and fees twice. | One entry per 15-minute window. `risk.decide` · test_05 |
| 18 | **Exits ignored depth.** Assumed the whole position sells at the best bid. | Overstated exit value; live partial fills recorded as full. | Sells only what the bid absorbs; partial fills split the position. `runner.check_exits`, `storage.split_bet` · test_07 |
| 19 | **Recorded planned price, not paid price.** | P&L and learning off by any slippage. | Reads the actual fill cost from Kalshi's order response. `engine.place` |
| 20 | **Half-filled arbitrage.** If the second leg failed, the first became a naked bet. | "Risk-free" trade turns into a directional one. | Both legs placed before recording; any filled leg is sold back immediately; urgent alert if that fails. `engine.unwind/execute_ladder` |
| 21 | **Slow web calls inside the 1-second live loop.** Kraken price was fetched on every research call. | Streams stall; decisions on lagging data; rate limits. | All slow lookups run in a background thread once a minute; the loop reads caches only. `runner.refresh_context` |

## D. Getting stuck or degrading silently

| # | Leak | Effect | Fix |
|---|---|---|---|
| 22 | **Permanent pause / bench.** With no new bets, a bad record never rolled off. | A league or the whole bot could stop forever. | Only the last 21 days count (`RECOVERY_DAYS`). `guard` · test_06 |
| 23 | **Model decay.** A promoted model could quietly stop working as markets change. | Loses money until someone notices. | Live monitor: if the model's forecasts are worse than Kalshi's over the last 300 settled markets, it's retired automatically (kept on disk) with an alert. `grader.monitor_model` |

## E. Found in the second pass

| # | Leak | Effect | Fix |
|---|---|---|---|
| 24 | **Silent WebSocket.** If the Kalshi feed couldn't log in or died quietly, books froze. | No trades at all, or trades on frozen prices. | Any book quiet for 8 s is re-fetched by REST, WebSocket or not. `runner.poll_books` · test_quiet_book_gets_polled |
| 25 | **One key for two jobs.** Demo trading needs a demo key, but the live feed needs a real-account key. | Demo mode would have broken the live price feed. | Separate `KALSHI_DATA_*` secrets (real account, read-only) from the trading key. `config.data_key` · test_data_key_separate |
| 26 | **Full size from day one.** Bets were sized normally before any track record existed. | Losses at full size while the bot is still unproven. | Size starts at 25% and grows to 100% only with a profitable graded record (200 bets). `risk.earned_size` · test_earned_size |
| 27 | **Stubborn maker orders.** If resting orders rarely fill, waiting for a better price loses the trade. | Missed edges. | Learns its own fill rate; below 25% it takes the ask instead. `storage.maker_fill_rate` |
| 28 | **Model on markets it never saw.** The 15-minute model could have priced hourly/daily contracts. | Garbage-in predictions. | The trained model is used only on 15-minute windows; ladders use the formula, a bigger edge requirement and smaller bets. `crypto_brain.research` |

## F. From the Council Lab design

| # | Leak | Effect | Fix |
|---|---|---|---|
| 29 | **Corrupted spot book.** No check that our Kraken book matched Kraken's. | Trading on a silently wrong order book. | CRC32 checksum on every update; mismatch = stale + reconnect. `live/book.verify` · test_checksum |
| 30 | **Hammering Kalshi after a 429.** Fixed short retries ignored Kalshi's requested wait. | Longer blocks; data outages. | Honors Retry-After with one shared cooldown and steady pacing; prices never cached. `kalshi_client._req` |
| 31 | **Spending money the account doesn't have.** No cash balance or position-count limits. | Over-exposure. | Cash floor, max open positions, max open cost, daily loss breaker. `risk.decide`, `guard.status` · TestAccountLimits |

## G. Third-pass reliability findings

| # | Leak | Effect | Fix |
|---|---|---|---|
| 32 | **Grading starvation.** The first bounded batch of unsettled or temporarily unavailable tickers could occupy every grading pass forever. | Settled markets later in the list never become learning examples, and model weights lag indefinitely. | Rotate through ungraded tickers with persistent cursors for signal learning, decision audits and combo audits; retry skipped rows on later passes. `grader.py` · `tests/test_grader_reliability.py` |
| 33 | **Invalid model-decay comparisons.** The decay monitor grouped by event instead of exact contract and mixed sports grades into a crypto-model health decision. | A few arbitrary strike rows or unrelated sports results could mask or falsely trigger crypto model demotion. | Store new grade segments by exact market ticker and compare paired `ml_model` / `kalshi_market` Brier scores only on 15-minute crypto contracts. Historical event-keyed rows remain less granular. `grader.py` · `tests/test_grader_reliability.py` |
| 34 | **Hard-stop bypass by direct command.** The CLI's `clear-pause` command previously removed the durable stop without requiring the workflow's manual confirmation. | A local or accidental invocation could resume order attempts while exchange orders/positions remain unreconciled. | Require `CONFIRM_RECONCILED=yes` and pass it only after the manual workflow's explicit YES input; update the prompt to require checking open orders and positions. `main.py` · `tests/test_clear_pause.py` |

Combo research remains explicitly opt-in through the existing `COMBO_RESEARCH_ENABLED` control; `COMBOS_ENABLED` remains off and is rejected by config validation because combo order execution is not implemented.

| 35 | **Dashboard/storage format mismatch.** The persistence workflow migrated the memory branch to `bot.sqlite.gz`, but the phone dashboard searched only for `bot.sqlite`. | The bot saved memory successfully while the existing dashboard reported no database and lost access to its research scorecards. | Dashboard data loading now prefers `bot.sqlite.gz`, decompresses it in-browser with the native gzip stream, retains compatibility with legacy `bot.sqlite`, and fails gracefully for unsupported browsers/oversized snapshots. No visual layout changes. `index.html` |

| 36 | **Insufficient training data looked like successful training.** The scheduler updated `last_train` even when the model returned `not enough data`. | Training could be silently postponed for another 24 hours despite the dataset still being below the required 300 windows; the workflow remained green without a new model. | Track attempt time and outcome separately, retry insufficient-data cases on a configurable 6-hour backoff, and keep the normal 24-hour cadence after a real training run. A missing legacy status triggers one migration attempt. `main.py` · `tests/test_training_safety.py` |

| 38 | **Detailed player-data collection was globally top-three, not league-fair.** The collector chose only three event summaries across every sport, so busy leagues could crowd out NFL/NCAA football, and the workflow did not expose the summary budget. | Injury, starter, depth-chart, player-stat and team-stat evidence could be missing for the user's selected leagues even while scoreboard/news counts looked healthy. | Increase the bounded default to six detailed summaries per refresh, prioritize coverage of selected manual leagues, rotate remaining slots across other categories, wire `SPORTS_INTEL_MAX_BOX_SCORES` into the workflow, and report team/player/injury/lineup profile counts. Missing fields remain unknown and do not independently create a bet. `sports_intel.py` · `tests/test_sports_intel.py` |

## What code can't remove
- **Competition.** Professional bots trade these markets; a real edge may not exist. The promotion gates and the live monitor are how you find out without paying for it.
- **Kalshi rules and fees change.** Check the fee rate (`TAKER_FEE_RATE`, `MAKER_FEE_RATE`), the averaging window (`SETTLE_AVG_SECONDS`), and the series tickers.
- **Unofficial feeds** (ESPN, Forex Factory) can change shape; they fail quietly and only remove context.
- **Repeated weekly testing** can still promote a lucky model eventually. The champion comparison and live demotion limit the damage; treat early live results with suspicion.
- **Platform risk.** GitHub runners can be delayed or killed; maker orders carry an expiry so none are left behind.
