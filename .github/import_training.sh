#!/usr/bin/env bash
# Apply the latest background-training delta (bot/train_delta.py) to the memory
# this bot run just restored. Runs once per delta; the bot run saves it as usual.
set -euo pipefail
if ! git fetch -q origin train-delta 2>/dev/null; then
  echo "No training delta published yet."
  exit 0
fi
rm -rf /tmp/td && mkdir -p /tmp/td
git archive origin/train-delta | tar -x -C /tmp/td
gzip -dc /tmp/td/delta.sqlite.gz > /tmp/td/delta.sqlite
python -m bot.main train-import /tmp/td/delta.sqlite /tmp/td/models
