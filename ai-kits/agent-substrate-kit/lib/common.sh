# shellcheck shell=bash
# agent-substrate-kit — shared helpers for kit-side scripts.
# Sourced by bootstrap.sh; not installed into projects.

if [ -t 1 ]; then
  _C_RED=$'\033[31m' _C_GRN=$'\033[32m' _C_YLW=$'\033[33m' _C_BLU=$'\033[34m' _C_BLD=$'\033[1m' _C_RST=$'\033[0m'
else
  _C_RED='' _C_GRN='' _C_YLW='' _C_BLU='' _C_BLD='' _C_RST=''
fi

info()  { printf '%s==>%s %s\n' "$_C_BLU" "$_C_RST" "$*"; }
ok()    { printf '%s ok %s %s\n' "$_C_GRN" "$_C_RST" "$*"; }
warn()  { printf '%swarn%s %s\n' "$_C_YLW" "$_C_RST" "$*" >&2; }
error() { printf '%s err%s %s\n' "$_C_RED" "$_C_RST" "$*" >&2; }
die()   { error "$@"; exit 2; }

have() { command -v "$1" >/dev/null 2>&1; }

# render_template <src> <dst>
# Replaces {{PLACEHOLDER}} tokens using the SUBST_* variables set by the caller:
#   SUBST_KIT_VERSION, SUBST_PROFILE, SUBST_LANGS, SUBST_PRIMARY_LANG, SUBST_PROJECT_NAME
render_template() {
  local src="$1" dst="$2"
  sed \
    -e "s|{{KIT_VERSION}}|${SUBST_KIT_VERSION:-}|g" \
    -e "s|{{PROFILE}}|${SUBST_PROFILE:-}|g" \
    -e "s|{{LANGS}}|${SUBST_LANGS:-}|g" \
    -e "s|{{PRIMARY_LANG}}|${SUBST_PRIMARY_LANG:-}|g" \
    -e "s|{{PROJECT_NAME}}|${SUBST_PROJECT_NAME:-}|g" \
    "$src" > "$dst"
}
