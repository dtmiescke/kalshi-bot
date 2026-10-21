#!/usr/bin/env bash
# Restore the bot's persistent memory before any job is allowed to run.
set -euo pipefail

mkdir -p data/models
rm -f data/.memory_restored

# Never treat a network/authentication failure as a fresh installation.
if ! git fetch -q origin bot-data; then
  echo "ERROR: Could not fetch persistent bot-data branch; refusing to run with empty memory." >&2
  exit 1
fi

rm -rf /tmp/bd
git worktree add -q /tmp/bd origin/bot-data

if [ ! -s /tmp/bd/bot.sqlite ]; then
  echo "ERROR: Persistent bot-data branch has no non-empty bot.sqlite; refusing to start fresh." >&2
  git worktree remove --force /tmp/bd || true
  exit 1
fi

cp /tmp/bd/bot.sqlite data/bot.sqlite
[ ! -f /tmp/bd/BRIEFING.txt ] || cp /tmp/bd/BRIEFING.txt data/BRIEFING.txt
[ ! -d /tmp/bd/models ] || cp -r /tmp/bd/models/. data/models/
git worktree remove --force /tmp/bd

# Check the restored database before marking memory as safe to use/save.
python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); result=c.execute("PRAGMA quick_check").fetchone()[0]; print("SQLite quick_check:", result); sys.exit(result != "ok")' data/bot.sqlite

# Save step requires this marker; a failed restore can never overwrite remote memory.
touch data/.memory_restored
echo "Persistent memory restored and validated."
