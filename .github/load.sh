#!/usr/bin/env bash
# Restore persistent memory, allowing a first-time bootstrap only when the remote
# is reachable and Git confirms that bot-data does not exist.
set -euo pipefail

mkdir -p data/models
rm -f data/.memory_restored

if git fetch -q origin bot-data; then
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

  # Validate the restored database before allowing it to be saved.
  python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); result=c.execute("PRAGMA quick_check").fetchone()[0]; print("SQLite quick_check:", result); sys.exit(result != "ok")' data/bot.sqlite
  touch data/.memory_restored
  echo "Persistent memory restored and validated."
  exit 0
fi

# A failed fetch alone is ambiguous (missing ref vs. network/auth failure).
# ls-remote must succeed AND confirm that this exact branch is absent before
# allowing the application to initialize a genuinely new database.
if remote_refs=$(git ls-remote --heads origin refs/heads/bot-data 2>/dev/null); then
  if [ -z "$remote_refs" ]; then
    touch data/.memory_restored
    echo "No bot-data branch exists on the reachable remote; allowing first-run initialization."
    exit 0
  fi
fi

echo "ERROR: Could not safely restore persistent bot-data; remote failure or ambiguous state. Refusing to run." >&2
exit 1
