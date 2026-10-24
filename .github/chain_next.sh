#!/usr/bin/env bash
# Queue the next bot cycle on main.
#   bash .github/chain_next.sh                 # from bot.yml after a saved cycle
#   bash .github/chain_next.sh after-training  # from train.yml when it finishes
# bot.yml and train.yml share one concurrency group, and a newly queued run
# replaces the one already waiting, so the bot steps aside while a trainer is
# waiting; train.yml restarts the chain when it finishes.
set -euo pipefail

repo="${GITHUB_REPOSITORY:?}"
# The app's pause button disables bot.yml. A paused bot must stay paused.
state=$(gh api "repos/$repo/actions/workflows/bot.yml" --jq .state)
if [ "$state" != "active" ]; then
  echo "bot.yml is $state; not queuing another run."
  exit 0
fi
if [ "${1:-}" != "after-training" ]; then
  waiting=$(gh run list -R "$repo" --workflow train.yml --limit 20 --json status \
    --jq '[.[] | select(.status == "queued" or .status == "waiting" or .status == "pending" or .status == "requested")] | length')
  if [ "${waiting:-0}" != "0" ]; then
    echo "Trainer is waiting; not queuing the next bot run (train.yml restarts the chain)."
    exit 0
  fi
fi
gh workflow run bot.yml -R "$repo" --ref main
echo "Next bot run queued on main."
