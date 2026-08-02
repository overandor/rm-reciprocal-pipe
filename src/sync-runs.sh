#!/usr/bin/env bash
# Pull the latest runs.json from the GitHub Actions repo so the local
# dashboard reflects cloud-run results. Run in a terminal alongside the
# local UI server. Polls every 30s by default.
#
# Usage:
#   ./sync-runs.sh            # one-shot pull
#   ./sync-runs.sh --watch    # poll every 30s
#   ./sync-runs.sh --watch 60 # poll every 60s
set -euo pipefail

REPO="overandor/rm-reciprocal-pipe"
BRANCH="main"
SRC_PATH="ui/runs.json"
DST_PATH="$(cd "$(dirname "$0")/.." && pwd)/ui/runs.json"

pull_once() {
  echo "[$(date +%H:%M:%S)] fetching $SRC_PATH from $REPO..."
  if gh api "repos/$REPO/contents/$SRC_PATH?ref=$BRANCH" \
        --jq '.content' | base64 -d > "$DST_PATH.tmp" 2>/dev/null; then
    mv "$DST_PATH.tmp" "$DST_PATH"
    echo "[$(date +%H:%M:%S)] updated $DST_PATH"
  else
    echo "[$(date +%H:%M:%S)] pull failed (repo empty or no runs yet?)"
    rm -f "$DST_PATH.tmp"
  fi
}

if [[ "${1:-}" == "--watch" ]]; then
  interval="${2:-30}"
  echo "Watching every ${interval}s. Ctrl-C to stop."
  while true; do
    pull_once
    sleep "$interval"
  done
else
  pull_once
fi
