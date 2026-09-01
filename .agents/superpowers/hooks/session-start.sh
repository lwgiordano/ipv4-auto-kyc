#!/usr/bin/env bash
# SessionStart hook: install the superpowers skills into ~/.claude/skills on a
# REMOTE (Claude Code on the web) session, pinned to a reviewed SHA. Fail-soft:
# a network/install failure warns and continues — a session without superpowers
# is degraded, never blocked. See design spec §5.
#
# Mechanics proven for audit finding F2:
#   - fetch BY URL (git init has no 'origin' remote — F1)
#   - SHA-aware, atomic-by-marker replacement (F4): the SHA marker is written
#     LAST, so "installed" == marker equals pin; a partial copy is retried
#   - reloadSkills:true so skills are usable in the SAME session (F2)
#   - only superpowers' own skill dirs are touched (never other user skills)

set -uo pipefail   # NOT -e: the network step must not abort the session

SUPERPOWERS_SHA="d884ae04edebef577e82ff7c4e143debd0bbec99"   # v6.1.1 (reviewed)
SUPERPOWERS_URL="https://github.com/obra/superpowers.git"
SKILLS_DIR="${HOME}/.claude/skills"
MARKER="${SKILLS_DIR}/.superpowers-sha"

emit() {  # $1 = reloadSkills (true|false), $2 = context message
  # Emit the SessionStart JSON contract. reloadSkills re-scans skill dirs so a
  # just-installed skill is usable this session (runtime >= 2.1.152).
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"},"reloadSkills":%s}\n' "$2" "$1"
}

# Remote-only: local dev sessions manage their own ~/.claude.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  emit false "superpowers: local session, hook skipped"
  exit 0
fi

# SHA-aware idempotence: already at the pinned SHA -> nothing to do, no reload.
if [ -f "$MARKER" ] && [ "$(cat "$MARKER" 2>/dev/null)" = "$SUPERPOWERS_SHA" ]; then
  emit false "superpowers: already at ${SUPERPOWERS_SHA:0:12}"
  exit 0
fi

install_superpowers() {
  local tmp staged
  tmp="$(mktemp -d)" || return 1
  staged="$(mktemp -d)" || { rm -rf "$tmp"; return 1; }
  # fetch BY URL (no named remote), pinned to the exact SHA
  git init -q "$tmp" || return 1
  git -C "$tmp" fetch --depth 1 "$SUPERPOWERS_URL" "$SUPERPOWERS_SHA" >/dev/null 2>&1 || return 1
  git -C "$tmp" checkout -q FETCH_HEAD >/dev/null 2>&1 || return 1
  [ -d "$tmp/skills" ] || return 1
  # stage a full copy first; only a complete stage is allowed to go live
  cp -R "$tmp/skills/." "$staged/" || return 1
  mkdir -p "$SKILLS_DIR" || return 1
  # replace ONLY superpowers' own skill dirs (leave other user skills intact)
  local d name
  for d in "$staged"/*/; do
    name="$(basename "$d")"
    rm -rf "${SKILLS_DIR:?}/${name}.tmp"
    cp -R "$d" "${SKILLS_DIR}/${name}.tmp" || return 1
    rm -rf "${SKILLS_DIR:?}/${name}"
    mv "${SKILLS_DIR}/${name}.tmp" "${SKILLS_DIR}/${name}" || return 1
  done
  printf '%s\n' "$SUPERPOWERS_SHA" > "$MARKER" || return 1   # commit point, written LAST
  rm -rf "$tmp" "$staged"
  return 0
}

if install_superpowers; then
  emit true "superpowers: installed ${SUPERPOWERS_SHA:0:12} (reload requested)"
else
  # fail-soft: warn on stderr, keep the session alive, no reload
  echo "superpowers session-start: install failed (degraded, non-fatal)" >&2
  emit false "superpowers: install failed, continuing without it"
fi
exit 0
