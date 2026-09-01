#!/usr/bin/env bash
# Shared-branch sync helper for the Claude <-> Codex handoff.
#
#   ./.agents/sync.sh pull    # get the other agent's latest commits (safe rebase)
#   ./.agents/sync.sh push    # publish your commits to the shared branch
#   ./.agents/sync.sh sync    # pull then push (do this after you commit)
#   ./.agents/sync.sh watch   # keep pulling every 30s — run in a spare terminal
#                             #   next to Codex so its folder auto-updates
#
# Override the branch/interval with env vars if needed:
#   SYNC_BRANCH=some/branch  SYNC_INTERVAL=15  ./.agents/sync.sh watch
set -euo pipefail

BRANCH="${SYNC_BRANCH:-claude/project-setup-verify-kpfgjs}"
INTERVAL="${SYNC_INTERVAL:-30}"
cmd="${1:-pull}"

pull() { git fetch origin "$BRANCH" && git pull --rebase --autostash origin "$BRANCH"; }
push() { git push origin "HEAD:$BRANCH"; }

case "$cmd" in
  pull)  pull ;;
  push)  push ;;
  sync)  pull && push ;;
  watch)
    echo "watching origin/$BRANCH every ${INTERVAL}s — Ctrl-C to stop"
    while true; do pull || echo "(pull failed — conflict? resolve, then it resumes)"; sleep "$INTERVAL"; done
    ;;
  *) echo "usage: $0 {pull|push|sync|watch}" >&2; exit 1 ;;
esac
