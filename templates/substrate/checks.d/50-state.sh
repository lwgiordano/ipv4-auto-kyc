#!/usr/bin/env bash
# doctor check: setup has run, and local substrate state stays out of git.
set -euo pipefail
cd "$SUBSTRATE_ROOT"

if [ -f "$SUBSTRATE_DIR/state/setup-done" ]; then
  when="$(sed -n 's/^completed_at=//p' "$SUBSTRATE_DIR/state/setup-done" | head -1)"
  echo "PASS setup has been run (${when:-time unknown})"
else
  echo "WARN setup has not been run yet — run ./manage.sh setup"
fi

if git check-ignore -q .substrate/state/setup-done 2>/dev/null; then
  echo "PASS .substrate/state/ is git-ignored"
else
  echo "WARN .substrate/state/ is not git-ignored — add '.substrate/state/' to .gitignore"
fi
