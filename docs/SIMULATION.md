# Simulated markets: what the formula does when we control the truth

Run with `python -m bot.simulate` (30,000 windows per row, seeded). A simulation can show what a formula does
*if* its assumptions hold. It cannot show that Kalshi is beatable; only the settled windows can.

## The model
Each window has a true probability `t`. Kalshi's price is `t` plus logit noise `sk` (the market's error). The
bot's forecast is `t` plus independent logit noise `sf`. The best belief is the precision-weighted mix, which is
Kalshi's price moved toward the forecast by

    lam* = sk^2 / (sk^2 + sf^2)

of the gap. Bet a side only if `win chance - ask - fee >= bar`, with ask = price + half the 2¢ spread + 0.5¢
slippage and the real fee (7% x p x (1-p)).

## Profit per $1 contract
| market error sk | bot error sf | lam* | trust forecast fully (bar 5¢) | lam* (bar 5¢) | best rule found |
|---|---|---|---|---|---|
| 0 (efficient) | 0.3 | 0 | -3.7¢ (5,778 bets) | no bets | -3.3¢ at best |
| 0.05 | 0.3 | 0.03 | -3.5¢ | no bets | -2.9¢ at best |
| 0.10 | 0.3 | 0.10 | -2.3¢ | no bets | -0.7¢ (bar 8¢, lam 0.75) |
| 0.20 | 0.3 | 0.31 | +0.6¢ (needs ~54,000 bets to see) | -1.1¢ on 25 bets | +4.7¢ (bar 2¢, lam 0.25, 304 bets, ~770 bets to see) |
| 0.20 | 0.6 | 0.10 | -1.7¢ | no bets | 0.0¢ |
| 0.30 | 0.3 | 0.50 | +3.4¢ | +7.9¢ (~280 bets to see) | +10.8¢ (bar 2¢, lam 0.25) |

## What it says
1. **If Kalshi is efficient, nothing can win.** Every rule loses 3-4¢ a contract in fees, spread and slippage;
   the only good outcome is not betting. More indicators do not change that.
2. **Trusting the forecast fully is the worst option in almost every world.** It only breaks even when the market
   is already off by about 0.2 logit and the forecast is sharp.
3. **The edge bar and the shrink interact.** After shrinking, the edge you can see is only `lam` of the raw gap,
   so a 5¢ bar on a shrunk belief almost never fires (rows with `lam*` show no bets). Where an edge exists, the
   best results used a small bar (2¢) with a moderate share (0.25).
4. **Slow to learn.** At the bot's current record (111 windows with a usable audit), the share of the forecast
   that is real is estimated at -0.92 +/- 0.65. Pinning it to +/-0.1 takes about 4,700 windows. The bot's own
   estimator (brain.shrink_weight) leans on a prior of 0.35 until then, and with ~1,000 simulated windows it
   still reads about 0.34 whether the truth is 0 or 0.1.

## What the real data adds
Kalshi's log loss is 0.0158 +/- 0.0112 above what its own prices would score if they were exactly right (1,041
windows, 1.4 standard errors). That is consistent with a market that is efficient or slightly noisy. We cannot
yet tell which, and the shrink share the bot learned has fallen to 0.13.

## Decision
No live setting was changed on the strength of a simulation. The gates already in place (shrink toward Kalshi's
price, proven-only playbook, slice gate, multiple-testing bars) match what the simulation says to do: stay out
unless a result survives. This module is the yardstick; `Insights` shows the real-data numbers.
