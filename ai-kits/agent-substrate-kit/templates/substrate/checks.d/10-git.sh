#!/usr/bin/env bash
# doctor check: the project is a git repository rooted where the substrate expects.
# Output protocol: lines starting with PASS/WARN/FAIL followed by a message.
set -euo pipefail
cd "$SUBSTRATE_ROOT"

if ! top="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "FAIL not inside a git repository (the substrate requires one)"
  exit 0
fi
top="$(cd "$top" && pwd -P)"
if [ "$top" = "$SUBSTRATE_ROOT" ]; then
  echo "PASS git repository root: $top"
else
  echo "FAIL substrate root ($SUBSTRATE_ROOT) is not the git toplevel ($top)"
fi
