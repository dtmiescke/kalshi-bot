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
if [ -z "${MEMORY_REMOTE:-}" ] && { [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; }; then
  echo "ERROR: Missing GitHub save credentials; persistent memory was not saved." >&2
  exit 1
fi
# MEMORY_REMOTE overrides the destination (tests use a local bare repository).
REMOTE="${MEMORY_REMOTE:-https://x-access-token:${GITHUB_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"
BASE_FILE="$(pwd)/data/.memory_base"

python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); result=c.execute("PRAGMA quick_check").fetchone()[0]; print("SQLite quick_check:", result); sys.exit(result != "ok")' data/bot.sqlite
python -m bot.main report > /tmp/REPORT.txt || { echo "WARNING: report failed; saving memory anyway." >&2; echo "report unavailable" > /tmp/REPORT.txt; }
python -m bot.main dashboard > /dev/null || echo "WARNING: dashboard failed; saving memory anyway." >&2

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# Store a compressed SQLite snapshot on bot-data to keep Git objects below
# GitHub's 50 MB recommended file size. load.sh still reads legacy bot.sqlite.
# SQLite's online backup API creates a transactionally consistent snapshot even
# if the application uses WAL mode. Never gzip the potentially live DB file.
python - "$WORK/bot.snapshot.sqlite" <<'PY'
import sqlite3
import sys

source = sqlite3.connect("file:data/bot.sqlite?mode=ro", uri=True)
try:
    with sqlite3.connect(sys.argv[1]) as target:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise RuntimeError(f"Snapshot integrity check failed: {result[:5]}")
finally:
    source.close()
PY
gzip -n -c "$WORK/bot.snapshot.sqlite" > "$WORK/bot.sqlite.gz"
rm -f "$WORK/bot.snapshot.sqlite"
# GitHub rejects files over 100 MB. Alert at 80% so the janitors' retention can
# be tightened before memory stops saving; refuse a push that would fail anyway.
SIZE=$(stat -c %s "$WORK/bot.sqlite.gz")
if [ "$SIZE" -ge 95000000 ]; then
  echo "ERROR: bot.sqlite.gz is $SIZE bytes, too close to GitHub's 100 MB file limit; not saving." >&2
  python -c 'import sys; from bot import alerts; alerts.send("Memory too big to save", sys.argv[1], priority="urgent", tags=["warning"])' "bot.sqlite.gz is $SIZE bytes; memory was NOT saved." || true
  exit 1
elif [ "$SIZE" -ge 80000000 ]; then
  echo "::warning::bot.sqlite.gz is $SIZE bytes (80% of GitHub's 100 MB file limit)."
  python -c 'import sys; from bot import alerts; alerts.send("Memory file near size limit", sys.argv[1], priority="high", tags=["warning"])' "bot.sqlite.gz is $SIZE bytes (limit 100 MB)." || true
fi
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
# Stale-base guard: bot-data must still be the snapshot this run restored. If
# another job (bot, trainer, clear-pause) saved in between, a force-push here
# would silently discard its newer memory, so refuse instead.
base=$(cat "$BASE_FILE" 2>/dev/null || echo missing)
if ! current=$(git ls-remote "$REMOTE" refs/heads/bot-data); then
  echo "ERROR: Could not read the current bot-data commit; refusing to overwrite memory." >&2
  exit 1
fi
current=$(printf '%s' "$current" | cut -f1)
if [ "$base" = "missing" ] || { [ "$base" = "none" ] && [ -n "$current" ]; } || \
   { [ "$base" != "none" ] && [ "$current" != "$base" ]; }; then
  echo "ERROR: bot-data moved since this run restored it (restored ${base:0:12}, now ${current:0:12}); refusing to overwrite newer memory." >&2
  python -c 'import sys; from bot import alerts; alerts.send("Memory save skipped", sys.argv[1], priority="high", tags=["warning"])' "bot-data changed during this run; this run's memory was not saved so the newer copy is kept." || true
  exit 1
fi
if git push -qf "$REMOTE" HEAD:bot-data; then
  # a later save in the same job (e.g. a mid-session save) builds on this one
  git rev-parse HEAD > "$BASE_FILE"
  echo "Persistent memory saved."
else
  echo "ERROR: Persistent memory push failed; remote memory was not confirmed saved." >&2
  exit 1
fi
