#!/usr/bin/env bash
# Save memory only if this run successfully restored the existing persistent state.
set -euo pipefail

if [ ! -f data/.memory_restored ]; then
  echo "ERROR: Persistent memory was not verified by load.sh; refusing to publish any database." >&2
  exit 1
fi
if [ ! -s data/bot.sqlite ]; then
  echo "ERROR: Database is missing or empty; refusing to overwrite persistent memory." >&2
  exit 1
fi
if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
  echo "ERROR: Missing GitHub save credentials; persistent memory was not saved." >&2
  exit 1
fi

python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); result=c.execute("PRAGMA quick_check").fetchone()[0]; print("SQLite quick_check:", result); sys.exit(result != "ok")' data/bot.sqlite
python -m bot.main report > /tmp/REPORT.txt
python -m bot.main dashboard > /dev/null

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp data/bot.sqlite "$WORK/bot.sqlite"
cp /tmp/REPORT.txt "$WORK/REPORT.txt"
[ ! -f data/BRIEFING.txt ] || cp data/BRIEFING.txt "$WORK/BRIEFING.txt"
[ ! -f data/live_summary.json ] || cp data/live_summary.json "$WORK/LIVE.json"
[ ! -d data/models ] || { mkdir -p "$WORK/models"; cp -r data/models/. "$WORK/models/"; }
[ ! -f data/models/crypto_model.json ] || cp data/models/crypto_model.json "$WORK/MODEL.json"
[ ! -d data/site ] || cp -r data/site/. "$WORK/"
[ ! -f data/DIAGNOSE.txt ] || cp data/DIAGNOSE.txt "$WORK/DIAGNOSE.txt"
touch "$WORK/.nojekyll"
cd "$WORK"
git init -q
git config user.name "kalshi-bot"
git config user.email "bot@users.noreply.github.com"
git add -A
git commit -qm "data $(date -u +%FT%TZ)"
if git push -qf "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" HEAD:bot-data; then
  echo "Persistent memory saved."
else
  echo "ERROR: Persistent memory push failed; remote memory was not confirmed saved." >&2
  exit 1
fi
