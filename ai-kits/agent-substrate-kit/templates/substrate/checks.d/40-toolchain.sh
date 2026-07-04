#!/usr/bin/env bash
# doctor check: required toolchain binaries exist for each configured language;
# recommended-but-optional tools produce warnings only.
set -euo pipefail
cd "$SUBSTRATE_ROOT"

have() { command -v "$1" >/dev/null 2>&1; }

req() {
  local lang="$1"; shift
  local bin
  for bin in "$@"; do
    if have "$bin"; then
      echo "PASS toolchain[$lang]: $bin available"
    else
      echo "FAIL toolchain[$lang]: required tool '$bin' not found on PATH"
    fi
  done
}

opt() {
  local lang="$1"; shift
  local bin
  for bin in "$@"; do
    if have "$bin"; then
      echo "PASS toolchain[$lang]: $bin available (optional)"
    else
      echo "WARN toolchain[$lang]: optional tool '$bin' not found (recommended)"
    fi
  done
}

for lang in ${SUBSTRATE_LANGS:-generic}; do
  case "$lang" in
    python)  req python python3; [ -x .venv/bin/ruff ] || opt python ruff ;;
    node)    req node node npm ;;
    go)      req go go ;;
    rust)    req rust cargo ;;
    java)    req java java ;;
    ruby)    req ruby ruby ;;
    shell)   req shell bash; opt shell shellcheck ;;
    generic) echo "PASS toolchain[generic]: nothing required" ;;
    *)       echo "WARN toolchain: unknown language '$lang' in substrate.conf" ;;
  esac
done
