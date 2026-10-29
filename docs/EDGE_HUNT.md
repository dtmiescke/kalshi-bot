# Edge hunt: 41 pre-registered rules on 14 days of real Kalshi history

Run: `python scripts/edge_hunt.py path/to/db.sqlite` on a database whose `ml_rows` hold replayed history
(`bot/history.py`: real Kalshi 1-minute candles plus Coinbase spot, features at every minute from 2 to 13.5
minutes before the close).

## Protocol (fixed before looking at any result)
- 41 rules written in advance: favorite/longshot, late certainty, momentum and reversal at three horizons, RSI and
  Bollinger extremes (both directions), time-of-day sessions, weekend, volatility regimes, the bot's own
  model-vs-Kalshi gap, coin baselines.
- One entry per window per rule, at the first minute the rule fires, bought at the ask + half a cent of slippage,
  with the real taker fee. Profit per $1 contract.
- Windows split by time: older 60% to discover, newer 40% to confirm.
- A rule PASSES only if, on the older windows, it has 100+ trades, a positive mean and a cluster-robust z (windows that
  close at the same time share a cluster) above the Bonferroni bar for 41 rules (z >= 3.03), AND on the newer windows it has
  a positive mean at z >= 2.0.

## Result (BTC, 1,328 windows, 796 discover / 532 confirm)
**0 of 41 passed.** ETH and SOL rules had no data (the downloader was rate limited), so they were not tested.

- Best on discovery: `follow_rsi_15m_extreme` +5.7¢ per contract (z 2.16, below the 3.03 bar). On the newer windows it
  was -0.5¢ (z -0.15): it did not hold.
- Most rules were negative on both halves. The worst on discovery, `fade_rsi_15m_extreme` (-10.6¢, z -4.1), is the
  mirror image of the best: that is the size of a pure-noise spread across a family of 41, not a signal.
- Nothing that looked good in the first half survived the second.

## What this does and does not say
It says that for these simple, well-known rules on this coin and these two weeks, Kalshi's price already contains what they
see, and fees plus spread make the rest negative. It does not prove no edge exists: a small one would need thousands of
windows to show, a different kind of information (order-flow, news, the lead-lag study) is not in these features, and ETH
and SOL are untested. The next honest steps are more windows (the live bot adds ~400 a day across coins) and running any
future candidate first on a coin it was not found on.

## Round 2 (three more angles, same honesty rules)

**Final two minutes (BTC, 1,328 windows, 8 rules fixed in advance, bar z >= 2.50).** Buying when the formula beats the
ask by 3, 6 or 10 points with 60-120 seconds left lost 1.4-3.2 cents per contract on both halves. These are mostly 3-cent
long shots that win about 7% of the time: the model overstates tail chances, and Kalshi's near-certain prices in the last
minutes are right. The 30-60 second window has no usable quotes. 0 of 8 passed.

**Scalping the lead-lag (280 spot jumps of 2+ standard deviations).** Kalshi does lag: after 2 seconds its mid has made 8% of
its eventual move, 20% after 5 seconds, 60% after 20 seconds. But the gap to close is small (median 3.8 cents against a 1-cent
spread), so buying the jump and selling 10-60 seconds later lost 1.1-3.7 cents per contract after both fees (z down to -10.8).
The lag is real; it is smaller than the round-trip cost. Older events store only mid prices, so exits were estimated as mid
minus half the spread; newer events store bid and ask.

**Daily crypto ladders (KXETHD, KXBTCD).** A first look showed the bot's forecast beating Kalshi's price at z 43-56 and a
98% win rate at +23 cents. That was an artifact: `market_price` is a mid, and on most rungs the real ask is far worse. Using
the bot's own edge, computed against the real ask and fee, only 44 markets had a tradable edge and they averaged +6.8 cents
at z 1.0. Not significant. Keep this in mind for any result that looks too good: check it against the real ask first.

## Round 3: what Kalshi lists beyond crypto, and a test on FX

**Public structure (API + rulebook only; Kalshi's internal code is not accessible and was not touched).** Of 1,093 financial
series, the frequent ones are 14 fifteen-minute, 33 hourly and 61 daily. Median volume per settled market (last 40):
S&P 500 daily up/down (KXINXDUD) 17,600 contracts, USDJPY 15-minute 8,400, EURUSD 15-minute 8,200, GBPUSD 4,600,
Nasdaq daily 4,200, USDCAD 3,900, AUDUSD 2,900, Treasury yields 15-minute 240-860. Most hourly index and yield series trade
nothing. FX 15-minute markets settle on the **Pyth Network** price (target = Pyth at the window start, `>=` so ties resolve
Yes); the S&P daily settles on the index close against the prior week's close. Pyth's historical endpoints now need
credentials, so the exact oracle series could not be replayed; Kalshi's settled markets publish the strike and settlement value.

**Pre-registered test (24 rules, bar z >= 2.87, 900 windows over 5 days: EURUSD, USDJPY, GBPUSD).** At 10, 7, 4 and 2 minutes
left: buy the long shot or the favorite when one side is 85+ cents, always Yes, always No, follow or fade a Kalshi price move of
8+ points over 3 minutes. **0 of 24 passed.** Nothing was positive on both halves. The always-No rule lost about $9-10 per
$100 bet on both halves at 4 and 2 minutes left (ties resolve Yes, and Yes was never cheaper), and fading price moves lost about
$11-13 per $100 late in the window. Costs and the Yes-on-tie rule explain a small part; the rest is that the price path is not
exploitable with these rules.

**Next honest step.** The FX markets are thick and settle on a free public oracle (Pyth Hermes streams live). The one thing not
tested is a live quote-lag study like the crypto one (record Pyth and Kalshi quotes each second, log would-be entries, settle
them), which needs the bot to read Pyth in the stream. That is a new collector, not a new rule.
