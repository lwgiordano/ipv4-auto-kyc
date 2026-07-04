#!/usr/bin/env bash
# ipv4-auto-kyc — project entry point.
# Installed by agent-substrate-kit v0.1.0 (profile: standard, languages: python).
# Re-running the kit's bootstrap.sh refreshes this file; put project-specific
# logic in AGENTS.md-documented scripts rather than editing this dispatcher.
set -euo pipefail

SUBSTRATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
if [ ! -f "$SUBSTRATE_ROOT/.substrate/lib.sh" ]; then
  echo "error: $SUBSTRATE_ROOT/.substrate/lib.sh not found — re-run the kit's bootstrap.sh" >&2
  exit 2
fi
# shellcheck source=/dev/null
. "$SUBSTRATE_ROOT/.substrate/lib.sh"
cd "$SUBSTRATE_ROOT"

cmd="${1:-help}"
[ $# -gt 0 ] && shift

case "$cmd" in
  setup)     sub_setup "$@" ;;
  doctor)    sub_doctor "$@" ;;
  fmt)       sub_fmt "$@" ;;
  lint)      sub_lint "$@" ;;
  test)      sub_test "$@" ;;
  info)      sub_info "$@" ;;
  update)    sub_update "$@" ;;
  precommit) sub_precommit "$@" ;;
  help|-h|--help) sub_help ;;
  *)
    echo "unknown command: $cmd" >&2
    sub_help >&2
    exit 64
    ;;
esac
