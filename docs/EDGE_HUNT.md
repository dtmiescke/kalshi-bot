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
