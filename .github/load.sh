#!/usr/bin/env bash
# Restore the bot's memory (database, trained models, briefing) from "bot-data".
set -u
mkdir -p data/models
if git fetch -q origin bot-data 2>/dev/null; then
  rm -rf /tmp/bd && git worktree add -q /tmp/bd origin/bot-data 2>/dev/null || exit 0
  [ -f /tmp/bd/bot.sqlite ] && cp /tmp/bd/bot.sqlite data/bot.sqlite
  [ -f /tmp/bd/BRIEFING.txt ] && cp /tmp/bd/BRIEFING.txt data/BRIEFING.txt
  [ -d /tmp/bd/models ] && cp -r /tmp/bd/models/. data/models/
  git worktree remove --force /tmp/bd
  echo "memory restored"
else
  echo "fresh start"
fi
