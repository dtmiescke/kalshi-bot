#!/usr/bin/env bash
# Weekly off-branch copy of the bot's memory as a GitHub Release asset
# (free, up to 2 GB per file). Keeps the newest BACKUP_KEEP (default 4) and
# deletes older memory-* releases and their tags. Read-only for the bot:
# it never touches bot-data or Hugging Face.
set -euo pipefail

db=${1:-data/bot.sqlite}
keep=${BACKUP_KEEP:-4}
repo=${GITHUB_REPOSITORY:?}
day=$(date -u +%Y-%m-%d)
tag="memory-$day"

if [ ! -s "$db" ]; then
  echo "ERROR: no memory at $db; nothing to back up." >&2
  exit 1
fi
mkdir -p /tmp/backup
out="/tmp/backup/bot-memory-$day.sqlite.gz"
gzip -c "$db" > "$out"
size=$(du -h "$out" | cut -f1)

if gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
  gh release upload "$tag" "$out" -R "$repo" --clobber
else
  gh release create "$tag" "$out" -R "$repo" --prerelease --title "Bot memory $day" \
    --notes "Weekly backup of the bot's memory ($size). Restore: gunzip, then put it at data/bot.sqlite."
fi
echo "Backed up memory ($size) to release $tag."

# newest first; drop everything past the newest $keep memory-* releases
gh release list -R "$repo" --limit 100 --json tagName,createdAt \
  --jq '[.[] | select(.tagName | startswith("memory-"))] | sort_by(.createdAt) | reverse | .[].tagName' \
  | tail -n +"$((keep + 1))" | while read -r old; do
    [ -n "$old" ] || continue
    gh release delete "$old" -R "$repo" --yes --cleanup-tag
    echo "Removed old backup $old."
  done
