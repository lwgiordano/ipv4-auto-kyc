#!/usr/bin/env bash
# doctor check: substrate core files are present, readable, and consistent.
set -euo pipefail
cd "$SUBSTRATE_ROOT"

if [ ! -r "$SUBSTRATE_CONF" ]; then
  echo "FAIL substrate.conf missing or unreadable — re-run bootstrap.sh"
  exit 0
fi
# shellcheck source=/dev/null
. "$SUBSTRATE_CONF"
echo "PASS substrate.conf readable"

if [ -n "${SUBSTRATE_KIT_VERSION:-}" ]; then
  echo "PASS kit version recorded: $SUBSTRATE_KIT_VERSION (profile: ${SUBSTRATE_PROFILE:-?})"
else
  echo "FAIL substrate.conf lacks SUBSTRATE_KIT_VERSION"
fi

if [ -x manage.sh ]; then
  echo "PASS manage.sh present and executable"
else
  echo "FAIL manage.sh missing or not executable"
fi

if [ ! -f "$SUBSTRATE_DIR/manifest" ]; then
  echo "FAIL .substrate/manifest missing — re-run bootstrap.sh"
else
  missing=0
  while read -r kind path; do
    [ -n "${path:-}" ] || continue
    if [ ! -e "$path" ]; then
      if [ "$kind" = "managed" ]; then
        echo "FAIL managed file missing: $path — re-run bootstrap.sh"
        missing=1
      else
        echo "WARN seeded file missing: $path (re-run bootstrap.sh to recreate)"
      fi
    fi
  done < "$SUBSTRATE_DIR/manifest"
  [ "$missing" -eq 0 ] && echo "PASS all managed files present"
fi
