#!/usr/bin/env bash
# Build the explicit staging source-handoff ZIP from the current clean commit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "error: run ./manage.sh setup first" >&2; exit 1; }

exec "$PY" "$ROOT/scripts/handoff/export.py" "$@"
