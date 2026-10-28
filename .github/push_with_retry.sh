#!/usr/bin/env bash
# Force-push HEAD to a branch, retrying uploads that stall or drop.
#   bash .github/push_with_retry.sh REMOTE BRANCH
# The memory file is ~80 MB; on 2026-10-28 a 3-hour stream lost every save to "curl 28 Operation
# too slow", with one attempt per save and a 30 s low-speed window. A forced push either lands or it
# doesn't, so repeating it is safe.
set -u
remote="${1:?remote}"
branch="${2:?branch}"
attempts="${SAVE_PUSH_ATTEMPTS:-3}"
for i in $(seq 1 "$attempts"); do
  if GIT_HTTP_LOW_SPEED_TIME="${PUSH_LOW_SPEED_TIME:-90}" \
     timeout --foreground "${GIT_PUSH_TIMEOUT_SECONDS:-420}s" git push --progress -f "$remote" "HEAD:$branch"; then
    exit 0
  fi
  echo "push attempt $i of $attempts failed" >&2
  if [ "$i" -lt "$attempts" ]; then
    sleep $(( ${SAVE_PUSH_BACKOFF:-20} * i ))
  fi
done
exit 1
