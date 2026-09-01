#!/usr/bin/env bash
# kyc-tool — project entry point (source-handoff edition).
#
# This is the self-contained version of the repository's developer dispatcher,
# shipped in the handoff package by scripts/package_handoff.sh. Same commands,
# no external tooling kit. Written for bash 3.2 so a stock macOS shell runs it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"

find_pgbin() {  # keep in sync with scripts/dev.sh
  if command -v initdb >/dev/null 2>&1; then
    dirname "$(command -v initdb)"
    return 0
  fi
  for candidate in /usr/lib/postgresql/16/bin /usr/lib/postgresql/15/bin \
      /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin \
      /opt/homebrew/opt/postgresql@15/bin \
      /Applications/Postgres.app/Contents/Versions/latest/bin \
      /usr/local/bin /usr/bin; do
    if [ -x "$candidate/initdb" ]; then echo "$candidate"; return 0; fi
  done
  return 1
}

sub_setup() {
  command -v python3 >/dev/null 2>&1 || { echo "error: python3 (3.11+) is required" >&2; exit 1; }
  if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "created .venv"
  fi
  .venv/bin/pip install -q --disable-pip-version-check -U pip
  # requirements.lock pins the runtime exactly (it is what the Docker image installs);
  # the dev extra adds the test/lint toolchain on top.
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.lock
  .venv/bin/pip install -q --disable-pip-version-check -e '.[dev]'
  echo "setup complete — run ./manage.sh doctor to check the rest of the toolchain"
}

sub_doctor() {
  fails=0
  if command -v python3 >/dev/null 2>&1; then
    echo "ok    python3: $(python3 --version 2>&1)"
  else
    echo "FAIL  python3 not found (3.11+ required)"; fails=1
  fi
  if [ -x "$PY" ]; then
    echo "ok    .venv present"
    if "$PY" -c 'import kyc_tool' 2>/dev/null; then
      echo "ok    kyc_tool imports from the venv"
    else
      echo "FAIL  kyc_tool does not import — re-run ./manage.sh setup"; fails=1
    fi
  else
    echo "FAIL  .venv missing — run ./manage.sh setup"; fails=1
  fi
  if pgbin="$(find_pgbin)"; then
    echo "ok    postgres binaries: $pgbin (needed only for the local demo stack, scripts/dev.sh)"
  else
    echo "warn  postgres binaries not found — only scripts/dev.sh needs them" \
         "(brew install postgresql@16 / apt install postgresql)"
  fi
  if command -v docker >/dev/null 2>&1; then
    echo "ok    docker present (needed only to build the deployment image)"
  else
    echo "warn  docker not found — only needed to build the deployment image"
  fi
  if [ "$fails" -ne 0 ]; then exit 1; fi
  echo "doctor: everything needed is in place"
}

sub_test() {
  [ -x "$PY" ] || { echo "error: no .venv — run ./manage.sh setup first" >&2; exit 1; }
  exec "$PY" -m pytest "$@"
}

sub_lint() {
  [ -x "$ROOT/.venv/bin/ruff" ] || { echo "error: no .venv — run ./manage.sh setup first" >&2; exit 1; }
  exec "$ROOT/.venv/bin/ruff" check "$@"
}

sub_help() {
  cat <<'EOF'
usage: ./manage.sh <command>

  setup      create .venv and install pinned dependencies + test toolchain
  doctor     check python/venv/postgres/docker and say what is missing
  test       run the test suite (extra args go to pytest)
  lint       run the linter (extra args go to ruff check)
  help       this text

The local demo stack (API + ops console + worker + throwaway Postgres) is
`bash scripts/dev.sh`; START-HERE.md is the map of the whole package.
EOF
}

cmd="${1:-help}"
[ $# -gt 0 ] && shift
case "$cmd" in
  setup)  sub_setup "$@" ;;
  doctor) sub_doctor "$@" ;;
  test)   sub_test "$@" ;;
  lint)   sub_lint "$@" ;;
  help|-h|--help) sub_help ;;
  *)
    echo "unknown command: $cmd" >&2
    sub_help >&2
    exit 64
    ;;
esac
