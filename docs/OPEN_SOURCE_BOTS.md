# What other Kalshi / crypto bots do, and what survived testing here

A survey of public projects (their code and write-ups; nothing proprietary was accessed), and a test of the ideas we did
not already have, under the same rules as everything else here: paper only, real costs, a strict bar for the number of
ideas tried.

## What they are
| Project | Strategies | Reported results |
|---|---|---|
| [hamad-khawaja/kalshi-trading-bot](https://github.com/hamad-khawaja/kalshi-trading-bot) (MIT) | 16-signal weighted sum blended 30% with a market anchor (min edge 5%, confidence 0.62, same-side edge on 2 cycles in a row, no entries in the first 7 or last minute, none 6 PM to 5 AM Eastern), market making on 15-minute BTC/ETH (needs a 5 to 8 cent spread), settlement ride (late entry, hold), certainty scalp (last 3 min, 85%+), Monte Carlo GBM. Kelly 0.20, 20% stop, trailing take-profit, exit 90 s before expiry | None published |
| [DeweyMarco/simple-kalshi-bot](https://www.mintlify.com/DeweyMarco/simple-kalshi-bot) | previous / momentum / consensus / two-leg arbitrage on KXBTC15M, genetic-algorithm tuning | None published |
| [13 agents write-up](https://dev.to/bearware/i-built-13-ai-agents-that-trade-kalshi-prediction-markets-247-heres-how-it-works-23k9) | 9-signal consensus (needs 6 of 9), Kraken lag arbitrage, sports from ESPN state | Self-reported: 521 trades, 58% wins, +$177; no period, no starting balance, product sold for $10 a month |
| Polymarket / Kalshi latency write-ups | cross-venue and spot-lag arbitrage | No verified P&L, no fee or slippage accounting; one concedes the gap shrinks as markets mature |
| [Hummingbot Avellaneda-Stoikov](https://hummingbot.org/blog/guide-to-the-avellaneda--stoikov-strategy/), Freqtrade/FreqAI | market making; ML strategies with hyperopt | Frameworks, not results; docs warn backtests do not reproduce |

No public bot shows audited profit. They use the same ingredients we already test (momentum, RSI, Bollinger, VWAP, order-book
imbalance, volume direction, Monte Carlo, certainty scalps, settlement rides, lag arbitrage, Kelly sizing, stop-losses).

## What we already covered and measured
Certainty scalp / late entry (final-two-minutes study: 0 of 8), settlement-style Monte Carlo (the formula is the settlement
average math), lag arbitrage (lead-lag study: real lag, smaller than round-trip costs), Kelly with an earned-size ramp,
stop-loss with a buffer, market making (live maker orders, auto fallback to taker below a 25% fill rate), session and
regime filters, weighted multi-signal consensus (the blend, learned weights, shrink toward Kalshi's price).

## What was new, and the result
Tested on the settled windows we hold, after fees, with the multiple-testing bar:

| Idea | Result |
|---|---|
| Same-side edge on two readings in a row (persistence) | Did not help: about -$11.7 per $100 bet, 55 entries |
| Skip a time-of-day session | **Bets placed 00-07 UTC lost about 22% per dollar (82 bets, z -3.4)**, past the whole-family bar. Direction agrees with that bot's overnight filter. The slice gate now covers it; it retests as old bets age out after 5 days |
| BTC leads ETH/SOL (follow BTC's strike distance, or BTC's 5-minute move) | Neither passed: -$6.6 then +$7.6 per $100 (strike distance), -$21.3 then +$17.9 (5-minute move); signs flipped between halves |
| Trailing take-profit / pre-expiry exit | Not testable on stored bets (no price path after entry); candidate for the live exit study |

## Takeaway
Nothing in the public bots is better than what is here on the evidence available; the difference is that this one measures
before it trusts. The one market-structure lever with support from both our data and a competitor is staying out of the
overnight session, which the gate now does automatically while the evidence stands.
