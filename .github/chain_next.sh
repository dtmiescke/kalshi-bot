#!/usr/bin/env bash
# Queue the next bot cycle on main.
#   bash .github/chain_next.sh                 # from bot.yml after a saved cycle
#   bash .github/chain_next.sh after-failure   # from bot.yml after a failed cycle
# kalshi-train no longer shares the bot's lock (it trains in the background and
# the bot imports its results), so the bot never steps aside for it.
set -euo pipefail

repo="${GITHUB_REPOSITORY:?}"
# The app's pause button disables bot.yml. A paused bot must stay paused.
state=$(gh api "repos/$repo/actions/workflows/bot.yml" --jq .state)
if [ "$state" != "active" ]; then
  echo "bot.yml is $state; not queuing another run."
  exit 0
fi
# after-failure: this run failed (e.g. a memory save timed out). Keep the bot
# going instead of waiting hours for GitHub's unreliable cron, but never loop:
# after 3 failures in a row, stop chaining and alert; the cron still retries.
if [ "${1:-}" = "after-failure" ]; then
  # this run is still in progress, so look at the completed ones before it
  recent=$(gh run list -R "$repo" --workflow bot.yml --limit 10 --json status,conclusion \
    --jq '[.[] | select(.status == "completed" and .conclusion != "cancelled")] | .[0:2] | map(.conclusion) | join(",")')
  if [ "$recent" = "failure,failure" ]; then
    echo "Third failed bot run in a row; not re-queuing (the 12-minute cron keeps retrying)."
    python -c 'from bot import alerts; alerts.send("Bot keeps failing", "3 bot runs failed in a row; automatic restarts paused until a run succeeds. Check the Actions log.", priority="high", tags=["warning"])' || true
    exit 0
  fi
  wait_s="${CHAIN_FAILURE_WAIT:-60}"
  echo "This run failed; queuing a fresh run in ${wait_s} s (recent: ${recent:-none})."
  sleep "$wait_s"
fi
gh workflow run bot.yml -R "$repo" --ref main
echo "Next bot run queued on main."
