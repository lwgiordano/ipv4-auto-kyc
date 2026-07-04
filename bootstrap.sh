#!/usr/bin/env bash
#
# agent-substrate-kit — bootstrap.sh
#
# Installs the agent substrate into the git repository that contains the
# current working directory: a ./manage.sh entry point, the .substrate/
# runtime (doctor checks, git hooks, config), and profile-dependent seed
# files (AGENTS.md, CLAUDE.md, .editorconfig, ...).
#
# Usage (run from inside the target project — it must be a git repo):
#   bash ~/ai-kits/agent-substrate-kit/bootstrap.sh [options]
#
# Options:
#   --profile <minimal|standard|full>  substrate profile        (default: standard)
#   --lang <auto|l1,l2,...>            project languages        (default: auto-detect)
#   --force                            overwrite seeded files and allow unusual
#                                      targets (e.g. the kit repo itself)
#   --dry-run                          show what would be installed, write nothing
#   --uninstall                        remove kit-managed files and hook wiring
#   -h, --help                         show this help
#
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
KIT_VERSION="$(tr -d '[:space:]' < "$KIT_DIR/VERSION")"

# shellcheck source=lib/common.sh
. "$KIT_DIR/lib/common.sh"
# shellcheck source=lib/detect.sh
. "$KIT_DIR/lib/detect.sh"

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

PROFILE=standard
LANG_SPEC=auto
FORCE=0
DRY_RUN=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) [ $# -ge 2 ] || die "--profile requires a value"; PROFILE="$2"; shift 2 ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    --lang) [ $# -ge 2 ] || die "--lang requires a value"; LANG_SPEC="$2"; shift 2 ;;
    --lang=*) LANG_SPEC="${1#*=}"; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------- target repo
if ! PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  die "bootstrap must be run from inside a git repository (the target project). Run 'git init' first, or cd into your project."
fi
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd -P)"
cd "$PROJECT_ROOT"
PROJECT_NAME="$(basename "$PROJECT_ROOT")"

if [ "$PROJECT_ROOT" = "$KIT_DIR" ] && [ "$FORCE" -ne 1 ]; then
  die "refusing to bootstrap the kit into its own repository (use --force if you really mean it)"
fi

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  info "uninstalling agent substrate from $PROJECT_ROOT"
  if [ -f .substrate/manifest ]; then
    while read -r kind path; do
      [ "$kind" = "managed" ] && [ -f "$path" ] && rm -f "$path" && ok "removed $path"
    done < .substrate/manifest
  fi
  rm -f manage.sh
  if [ "$(git config --local --get core.hooksPath 2>/dev/null || true)" = ".substrate/hooks" ]; then
    git config --local --unset core.hooksPath
    ok "unset core.hooksPath"
  fi
  rm -rf .substrate
  ok "removed .substrate/"
  info "seeded files (AGENTS.md, CLAUDE.md, .editorconfig, ...) were left in place"
  exit 0
fi

# -------------------------------------------------------------------- profile
PROFILE_FILE="$KIT_DIR/profiles/$PROFILE.profile"
if [ ! -f "$PROFILE_FILE" ]; then
  available=""
  for p in "$KIT_DIR"/profiles/*.profile; do
    available="$available$(basename "$p" .profile) "
  done
  die "unknown profile '$PROFILE' (available: $available)"
fi
# shellcheck disable=SC1090
. "$PROFILE_FILE"

# ------------------------------------------------------------------ languages
if [ "$LANG_SPEC" = "auto" ]; then
  LANGS="$(detect_languages)"
  LANG_ORIGIN="auto-detected"
else
  LANGS="$(printf '%s' "$LANG_SPEC" | tr ',' ' ')"
  LANG_ORIGIN="explicit"
fi
validate_langs "$LANGS"
PRIMARY_LANG="${LANGS%% *}"

info "agent-substrate-kit v$KIT_VERSION"
info "target   : $PROJECT_ROOT"
info "profile  : $PROFILE"
info "languages: $LANGS ($LANG_ORIGIN)"
[ "$DRY_RUN" -eq 1 ] && info "mode     : dry run — nothing will be written"

# ------------------------------------------------------------- template setup
SUBST_KIT_VERSION="$KIT_VERSION"
SUBST_PROFILE="$PROFILE"
SUBST_LANGS="$LANGS"
SUBST_PRIMARY_LANG="$PRIMARY_LANG"
SUBST_PROJECT_NAME="$PROJECT_NAME"

MANIFEST_LINES=()

# install_managed <template relpath> <dest relpath> <mode>
# Kit-owned files: always refreshed on re-run.
install_managed() {
  local tpl="$1" dst="$2" mode="$3"
  MANIFEST_LINES+=("managed $dst")
  if [ "$DRY_RUN" -eq 1 ]; then ok "would install $dst"; return 0; fi
  mkdir -p "$(dirname "$dst")"
  render_template "$KIT_DIR/templates/$tpl" "$dst"
  chmod "$mode" "$dst"
  ok "installed $dst"
}

# install_seed <template relpath> <dest relpath>
# User-owned starting points: created once, never overwritten (unless --force).
install_seed() {
  local tpl="$1" dst="$2"
  MANIFEST_LINES+=("seeded $dst")
  if [ -e "$dst" ] && [ "$FORCE" -ne 1 ]; then
    info "kept existing $dst"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then ok "would seed $dst"; return 0; fi
  mkdir -p "$(dirname "$dst")"
  render_template "$KIT_DIR/templates/$tpl" "$dst"
  ok "seeded $dst"
}

# ------------------------------------------------------------------- hooks?
HOOKS_ENABLED=0
if [ "${PROFILE_HOOKS:-0}" -eq 1 ]; then
  EXISTING_HOOKS="$(git config --local --get core.hooksPath 2>/dev/null || true)"
  if [ -n "$EXISTING_HOOKS" ] && [ "$EXISTING_HOOKS" != ".substrate/hooks" ] && [ "$FORCE" -ne 1 ]; then
    warn "core.hooksPath already set to '$EXISTING_HOOKS' — leaving it alone (use --force to take over)"
  else
    HOOKS_ENABLED=1
  fi
fi

# -------------------------------------------------------------------- install
install_managed manage.sh                        manage.sh                        755
install_managed substrate/lib.sh                 .substrate/lib.sh                644
install_managed substrate/checks.d/10-git.sh     .substrate/checks.d/10-git.sh    644
install_managed substrate/checks.d/20-substrate.sh .substrate/checks.d/20-substrate.sh 644
install_managed substrate/checks.d/30-hooks.sh   .substrate/checks.d/30-hooks.sh  644
install_managed substrate/checks.d/40-toolchain.sh .substrate/checks.d/40-toolchain.sh 644
install_managed substrate/checks.d/50-state.sh   .substrate/checks.d/50-state.sh  644
install_managed substrate/hooks/pre-commit       .substrate/hooks/pre-commit      755

[ "${PROFILE_AGENTS_MD:-0}" -eq 1 ]      && install_seed AGENTS.md            AGENTS.md
[ "${PROFILE_CLAUDE_MD:-0}" -eq 1 ]      && install_seed CLAUDE.md            CLAUDE.md
[ "${PROFILE_EDITORCONFIG:-0}" -eq 1 ]   && install_seed editorconfig         .editorconfig
[ "${PROFILE_CLAUDE_SETTINGS:-0}" -eq 1 ] && install_seed claude-settings.json .claude/settings.json
[ "${PROFILE_CI:-0}" -eq 1 ]             && install_seed ci.yml               .github/workflows/substrate-ci.yml

if [ "$DRY_RUN" -eq 1 ]; then
  info "dry run complete — nothing was written"
  exit 0
fi

mkdir -p .substrate/state

# substrate.conf — single source of truth for the installed substrate.
{
  echo "# Written by agent-substrate-kit bootstrap.sh — do not edit by hand."
  echo "SUBSTRATE_KIT_VERSION=\"$KIT_VERSION\""
  echo "SUBSTRATE_PROFILE=\"$PROFILE\""
  echo "SUBSTRATE_LANGS=\"$LANGS\""
  echo "SUBSTRATE_PRIMARY_LANG=\"$PRIMARY_LANG\""
  echo "SUBSTRATE_HOOKS=\"$HOOKS_ENABLED\""
  echo "SUBSTRATE_KIT_SOURCE=\"$KIT_DIR\""
  echo "SUBSTRATE_INSTALLED_AT=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
} > .substrate/substrate.conf
ok "wrote .substrate/substrate.conf"

printf '%s\n' "${MANIFEST_LINES[@]}" > .substrate/manifest
ok "wrote .substrate/manifest"

if [ "$HOOKS_ENABLED" -eq 1 ]; then
  git config --local core.hooksPath .substrate/hooks
  ok "wired git hooks (core.hooksPath -> .substrate/hooks)"
fi

# Keep local substrate state — and the artifacts `manage.sh setup` creates —
# out of version control.
ensure_gitignore() {
  local entry="$1" header='# agent-substrate-kit local artifacts'
  if ! grep -qxF "$entry" .gitignore 2>/dev/null; then
    if ! grep -qxF "$header" .gitignore 2>/dev/null; then
      [ -s .gitignore ] && printf '\n' >> .gitignore
      printf '%s\n' "$header" >> .gitignore
    fi
    printf '%s\n' "$entry" >> .gitignore
    ok "added $entry to .gitignore"
  fi
}

ensure_gitignore '.substrate/state/'
for _lang in $LANGS; do
  case "$_lang" in
    python) ensure_gitignore '.venv/'; ensure_gitignore '__pycache__/' ;;
    node)   ensure_gitignore 'node_modules/' ;;
    rust)   ensure_gitignore 'target/' ;;
  esac
done

printf '\n'
info "substrate installed into $PROJECT_NAME (profile: $PROFILE, languages: $LANGS)"
info "next steps:"
printf '      ./manage.sh setup     # install deps / prepare toolchain\n'
printf '      ./manage.sh doctor    # verify wiring\n'
