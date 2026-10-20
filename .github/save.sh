#!/usr/bin/env bash
# Save the bot's memory + reports to the "bot-data" branch.
set -u
[ -f data/bot.sqlite ] || { echo "nothing to save"; exit 0; }
python -m bot.main report > /tmp/REPORT.txt 2>/dev/null || true
python -m bot.main dashboard > /dev/null 2>&1 || true
WORK=$(mktemp -d)
cp data/bot.sqlite "$WORK/bot.sqlite"
cp /tmp/REPORT.txt "$WORK/REPORT.txt"
[ -f data/BRIEFING.txt ] && cp data/BRIEFING.txt "$WORK/BRIEFING.txt"
[ -f data/live_summary.json ] && cp data/live_summary.json "$WORK/LIVE.json"
[ -d data/models ] && mkdir -p "$WORK/models" && cp -r data/models/. "$WORK/models/"
[ -f data/models/crypto_model.json ] && cp data/models/crypto_model.json "$WORK/MODEL.json"
[ -d data/site ] && cp -r data/site/. "$WORK/"      # index.html + bets.csv for GitHub Pages
[ -f data/DIAGNOSE.txt ] && cp data/DIAGNOSE.txt "$WORK/DIAGNOSE.txt"
touch "$WORK/.nojekyll"
cd "$WORK"
git init -q
git config user.name "kalshi-bot"
git config user.email "bot@users.noreply.github.com"
git add -A
git commit -qm "data $(date -u +%FT%TZ)"
git push -qf "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" HEAD:bot-data \
  && echo "saved" || echo "save failed"
