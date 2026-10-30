#!/usr/bin/env bash
# Stop date for the bot. RUN_UNTIL is an ISO UTC time (repository variable APP_RUN_UNTIL); once it has passed this
# disables the workflow so the cron fallback stops too, and cancels the current run (a cancelled run never re-queues).
# RUN_UNTIL empty or "never" means no stop date. To resume later: Actions tab -> kalshi-bot -> Enable workflow, and set
# APP_RUN_UNTIL to a later date (or "never"). DRY_RUN=1 only prints the decision. NOW can override the clock for tests.
set -u
until="${RUN_UNTIL:-}"
if [ -z "$until" ] || [ "$until" = "never" ]; then
  echo "go (no stop date)"
  exit 0
fi
now="${NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
# ISO timestamps in the same format compare correctly as strings
if [[ "$now" < "$until" ]]; then
  echo "go (now $now is before the stop date $until)"
  exit 0
fi
echo "stop (now $now is at or after the stop date $until)"
if [ "${DRY_RUN:-0}" = "1" ]; then
  exit 0
fi
gh workflow disable "${WORKFLOW_FILE:-bot.yml}" || true
gh run cancel "${GITHUB_RUN_ID:?}" || true
sleep 60
exit 1
