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
| 12 min | 8-minute **live streaming burst** on BTC/ETH/SOL 15-minute markets, grading, learning, AI briefing |
| 5.5 h | full **sports** scan (inside one 12-minute run) |
| daily | **retrain** the model on all history + live decisions; promote only if it wins on unseen data |
| weekly | download fresh real Kalshi history |
| daily | trim raw data so the memory file stays small (learning is never trimmed) |

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

## AI: OpenRouter and/or OmniRoute
Every AI feature (news panel, council, briefings) speaks the OpenAI-compatible format, so it
works with OpenRouter (default) or a self-hosted OmniRoute gateway. With `OMNIROUTE_URL`
set, OmniRoute is tried first; if it's unreachable, calls fall back to OpenRouter.
`python -m bot.main check` sends a one-word test to confirm which one answered.

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
