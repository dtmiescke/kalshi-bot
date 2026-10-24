# Phase 6 - The Council + Don Theo

## Ground rules
- New branch, PR, never commit to main. Paper mode. Zero cost; all AI behind a hard daily cap.
- bot/don_theo.py (line bank, moods, streaks, roasts) and bot/council.py already exist: BUILD ON THEM, don't duplicate.
- The Council starts ADVISORY: the math (risk.decide) makes the call. Power is earned (below).

## The Boss: Don Theo "Bones" The Rott Master
- Council tab header: "The Rott Master's Table". Signature: "Never doubt the nose."
- Council calls him "Rott Master" normally, "Don Theo" when there's trouble.
- Look: Rottweiler, gold chains, cigar, high-back leather chair. Stands out from the five robot-style members.
- Voice: mob-boss + dog puns, short, punchy, always attaches the REAL reason.

### The rule he never breaks
Jokes never hide bad news. Losing money or a source down -> say it plainly FIRST, joke second. "We're down $14 this week. That's real. Now let's figure out why."

### Moods (from real results; shown in cigar glow, posture, table-tap force)
- Smooth: winning week - relaxed, smoke rings, jokes
- All business: break-even - short, sharp
- Grumpy: losing streak - snaps at the Council, demands bigger edges
- Hot hand: 5+ wins in a row - cocky, shades on: "Somebody get me a bigger chair."
- Wins: cocky, scaled to size/streak. Losses: humbled.

### Nicknames
Sharp "Numbers", Scout "Eyes", Situationist "Professor", Market Reader "Ticker", Stats Analyst "Calculator".

### Seating / relationships
- Best recent record sits closest and gets praised.
- Doghouse: cold-streak member is roasted and drops back a seat until earned back.
- Calls out cold streaks: "Situationist, you're 2 for your last 9. You're on thin ice, pal."
- "I told you so" roasts for members who voted against a winner.

### Memory callbacks
Pulls from the casebook: "Last time we backed a road team on a back-to-back, we got burned. Professor, convince me this one's different."

### Sample lines (fallback bank style)
- "Sharp, go sniff out the line. Scout, I wanna know who's actually playin'."
- "Everybody earns their kibble today. Get to work."
- Win: "Like takin' a squeaky toy from a puppy." / "Told ya. Never doubt the nose."
- Loss: "Eh. Even the best dog misses a squirrel sometimes." / "Somebody's gettin' a talkin' to at the next meeting."
- Best day: "Pour me somethin' nice." Chains: "Real gold. Like my picks." Sunday: "Numbers carried us. Ticker, we need to talk."

### Animations (CSS/SVG, light, phone-friendly)
Idle cigar puff; point at a member; table tap on final call; approve = slow nod + chains glint; veto = stubs out cigar + head shake; win = smoke ring ($-shaped on big wins); chain flex; feet up in good mood; two cigars on best day; confetti on big wins; shades in Hot hand.

### Interactions
Tap him: comment on today's slate. Tap chains: flex line. Optional voice via phone text-to-speech (Web Speech API), off by default, toggle in Control.

### Sunday Sit-Down
Weekly recap in his voice: record, profit/loss, best and worst call, doghouse, who earned a better seat. Bad news first if the week lost money.

### How lines are made
AI-written from REAL bet details via OpenRouter through bot/llm.py with a hard daily cap. Fallback to don_theo.py's line bank filled with real numbers when budget is out or a call fails. Never blank. Every line includes the real reason/number.

## The Council of 5 (+ Quant for crypto)
1 Sharp (Numbers) - sharp lines, movement, value vs price
2 Stats Analyst (Calculator) - stats, models, projections
3 Scout (Eyes) - injuries, lineups, news
4 Situationist (Professor) - rest, travel, weather, venue, matchups
5 Market Reader (Ticker) - Kalshi book, trade flow, price action
Quant takes a seat for crypto (volatility, settlement math, momentum).

### Debate flow (TradingAgents-style)
- Meets only on real candidates that already passed the math's edge check.
- Cheap triage first (quick-think model); full bull-vs-bear debate only when it matters (deep-think model).
- Each member: vote (back/pass), confidence, one-sentence reason. Then Don Theo rules and explains.
- Hard daily AI cap; members fall back to rule-based reasons from their data.
- Save votes, reasons, and ruling with the bet/pass record (why card).

### Grading
After settlement each vote is scored (right/wrong + Brier on confidence). Rolling record drives seating, praise/roasts, Doghouse, Sunday Sit-Down.

### Earned power
Advisory -> Veto -> Full vote, based on graded record (min graded votes + beats the market). Thresholds are settings. Demotion if the record drops.

## App (Council tab)
- Chamber (ground floor): table, members seated by record, Doghouse seat, latest line, mood.
- Elevator -> Data Floor: one bot per source with health light and flow lines, plus 2 Janitor bots and the Nerd Watchdog, each looking like what it's called.
- HQ shows his latest line + status light. Why card per bet/pass. Sunday Sit-Down card.
- No overlapping labels at 380px. Load from small summary JSON.

## Acceptance checks
- Bad-news-first enforced (test).
- Each mood picked correctly incl. 5-win Hot hand (tests).
- Fallback lines when AI cap hit; never blank (test).
- Advisory stage never changes a bet decision (test).
- Member grading updates after settlement (test).
- All existing tests pass; PR opened, not merged.
