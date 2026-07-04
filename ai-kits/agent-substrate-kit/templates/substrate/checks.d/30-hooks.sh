#!/usr/bin/env bash
# doctor check: git hook wiring matches what bootstrap configured.
set -euo pipefail
cd "$SUBSTRATE_ROOT"

if [ "${SUBSTRATE_HOOKS:-0}" != "1" ]; then
  echo "PASS git hooks not managed by substrate (profile: ${SUBSTRATE_PROFILE:-?})"
  exit 0
fi

hp="$(git config --local --get core.hooksPath 2>/dev/null || true)"
if [ "$hp" = ".substrate/hooks" ]; then
  echo "PASS core.hooksPath wired to .substrate/hooks"
else
  echo "FAIL core.hooksPath is '${hp:-unset}' (expected .substrate/hooks) — re-run bootstrap.sh"
fi

if [ -x .substrate/hooks/pre-commit ]; then
  echo "PASS pre-commit hook present and executable"
else
  echo "FAIL pre-commit hook missing or not executable — re-run bootstrap.sh"
fi
