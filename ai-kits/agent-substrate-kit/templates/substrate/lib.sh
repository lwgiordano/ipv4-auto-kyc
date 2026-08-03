# shellcheck shell=bash
# .substrate/lib.sh — agent substrate runtime.
# Installed by agent-substrate-kit v{{KIT_VERSION}}; re-running bootstrap.sh
# refreshes this file, so keep project-specific logic out of it.
# Sourced by ./manage.sh with SUBSTRATE_ROOT set to the project root.

SUBSTRATE_DIR="$SUBSTRATE_ROOT/.substrate"
SUBSTRATE_CONF="$SUBSTRATE_DIR/substrate.conf"

if [ -t 1 ]; then
  _C_RED=$'\033[31m' _C_GRN=$'\033[32m' _C_YLW=$'\033[33m' _C_BLU=$'\033[34m' _C_RST=$'\033[0m'
else
  _C_RED='' _C_GRN='' _C_YLW='' _C_BLU='' _C_RST=''
fi

info()  { printf '%s==>%s %s\n' "$_C_BLU" "$_C_RST" "$*"; }
ok()    { printf '%s ok %s %s\n' "$_C_GRN" "$_C_RST" "$*"; }
warn()  { printf '%swarn%s %s\n' "$_C_YLW" "$_C_RST" "$*" >&2; }
error() { printf '%s err%s %s\n' "$_C_RED" "$_C_RST" "$*" >&2; }
die()   { error "$@"; exit 2; }

have() { command -v "$1" >/dev/null 2>&1; }

load_conf() {
  [ -f "$SUBSTRATE_CONF" ] || die "missing $SUBSTRATE_CONF — re-run the kit's bootstrap.sh"
  set -a
  # shellcheck source=/dev/null
  . "$SUBSTRATE_CONF"
  set +a
}

sub_help() {
  cat <<'EOF'
usage: ./manage.sh <command>

commands:
  setup      install dependencies and prepare the toolchain
  doctor     verify the substrate wiring (exit 1 if anything is broken)
  fmt        format the code
  lint       lint the code
  test       run the test suite
  info       show substrate state (profile, languages, versions)
  update     re-run bootstrap from the recorded kit source
  precommit  what the pre-commit hook runs (lint, soft by default)
  help       this message
EOF
}

# ------------------------------------------------------------------- setup --

sub_setup() {
  load_conf
  info "setup: profile=$SUBSTRATE_PROFILE, languages=$SUBSTRATE_LANGS"
  local l rc=0
  for l in $SUBSTRATE_LANGS; do
    if ! "setup_$l"; then
      error "setup failed for language: $l"
      rc=1
    fi
  done
  [ "$rc" -eq 0 ] || die "setup did not complete — fix the errors above and re-run ./manage.sh setup"
  # Durable hook wiring (PR 10a, corrected per re-audit f2929f8..6a4cd87 F13 — the kit template is
  # the authority, never the installed copy): the CANONICAL literal `.substrate/hooks` (what doctor
  # 30-hooks.sh expects), `-e .git` so worktrees (where .git is a file) work, and a pre-existing
  # NON-KIT hooksPath is preserved rather than clobbered.
  if [ -e .git ] && [ -d .substrate/hooks ]; then
    current="$(git config --local --get core.hooksPath 2>/dev/null || true)"
    if [ -z "$current" ] || [ "$current" = ".substrate/hooks" ]; then
      git config core.hooksPath .substrate/hooks && ok "wired core.hooksPath -> .substrate/hooks"
    else
      info "core.hooksPath already set to '$current' — leaving it (non-kit hooks preserved)"
    fi
  fi
  mkdir -p "$SUBSTRATE_DIR/state"
  {
    echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "languages=$SUBSTRATE_LANGS"
  } > "$SUBSTRATE_DIR/state/setup-done"
  ok "setup complete — run ./manage.sh doctor to verify wiring"
}

setup_python() {
  have python3 || { error "python3 is required but not found"; return 1; }
  if [ ! -d .venv ]; then
    if python3 -m venv .venv >/dev/null 2>&1; then
      ok "created .venv"
    else
      warn "could not create .venv (python3-venv missing?) — falling back to system python"
    fi
  fi
  local -a pip=(python3 -m pip)
  [ -x .venv/bin/pip ] && pip=(.venv/bin/pip)
  if [ -f requirements.txt ]; then
    if "${pip[@]}" install -q --disable-pip-version-check -r requirements.txt; then
      ok "installed requirements.txt"
    else
      warn "pip install -r requirements.txt failed — install dependencies manually"
    fi
  elif [ -f pyproject.toml ]; then
    if "${pip[@]}" install -q --disable-pip-version-check -e . >/dev/null 2>&1; then
      ok "installed project in editable mode"
    else
      warn "editable install from pyproject.toml failed — install dependencies manually if needed"
    fi
  else
    info "python: no requirements.txt or pyproject.toml — nothing to install"
  fi
}

setup_node() {
  have node || { error "node is required but not found"; return 1; }
  if [ -f pnpm-lock.yaml ] && have pnpm; then
    pnpm install --silent || warn "pnpm install failed — install dependencies manually"
  elif [ -f yarn.lock ] && have yarn; then
    yarn install --silent || warn "yarn install failed — install dependencies manually"
  elif have npm; then
    if [ -f package-lock.json ]; then
      npm ci --no-audit --no-fund --silent \
        || npm install --no-audit --no-fund --silent \
        || warn "npm install failed — install dependencies manually"
    else
      npm install --no-audit --no-fund --silent \
        || warn "npm install failed — install dependencies manually"
    fi
  else
    error "npm is required but not found"
    return 1
  fi
  ok "node dependencies ready"
}

setup_go() {
  have go || { error "go is required but not found"; return 1; }
  go mod download 2>/dev/null || warn "go mod download failed — fetch modules manually"
  ok "go modules ready"
}

setup_rust() {
  have cargo || { error "cargo is required but not found"; return 1; }
  cargo fetch --quiet 2>/dev/null || warn "cargo fetch failed — fetch crates manually"
  ok "cargo dependencies ready"
}

setup_java() {
  have java || { error "java is required but not found"; return 1; }
  if [ -x ./mvnw ] || [ -x ./gradlew ]; then
    info "java: build wrapper detected — dependencies resolve on first build"
  else
    info "java: no mvnw/gradlew wrapper — use your build tool to resolve dependencies"
  fi
}

setup_ruby() {
  have ruby || { error "ruby is required but not found"; return 1; }
  if [ -f Gemfile ] && have bundle; then
    bundle install --quiet || warn "bundle install failed — install gems manually"
    ok "ruby gems ready"
  else
    info "ruby: no Gemfile/bundler — nothing to install"
  fi
}

setup_shell() {
  info "shell: nothing to install"
  have shellcheck || warn "shellcheck not found — recommended for ./manage.sh lint"
}

setup_generic() {
  info "generic: no language-specific setup (set languages with bootstrap.sh --lang, or edit AGENTS.md)"
}

# ------------------------------------------------------------------ doctor --

sub_doctor() {
  load_conf
  local checks_dir="$SUBSTRATE_DIR/checks.d"
  [ -d "$checks_dir" ] || die "missing $checks_dir — re-run the kit's bootstrap.sh"
  export SUBSTRATE_ROOT SUBSTRATE_DIR SUBSTRATE_CONF
  info "doctor: verifying substrate wiring in $SUBSTRATE_ROOT"
  local check out rc status text passes=0 warns=0 fails=0
  for check in "$checks_dir"/*.sh; do
    [ -e "$check" ] || continue
    rc=0
    out="$(bash "$check" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
      printf '%s[FAIL]%s check %s exited with status %s\n' "$_C_RED" "$_C_RST" "$(basename "$check")" "$rc"
      fails=$((fails + 1))
    fi
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      status="${line%% *}"
      text="${line#* }"
      case "$status" in
        PASS) passes=$((passes + 1)); printf '%s[ ok ]%s %s\n' "$_C_GRN" "$_C_RST" "$text" ;;
        WARN) warns=$((warns + 1));  printf '%s[warn]%s %s\n' "$_C_YLW" "$_C_RST" "$text" ;;
        FAIL) fails=$((fails + 1));  printf '%s[FAIL]%s %s\n' "$_C_RED" "$_C_RST" "$text" ;;
        *)    printf '       %s\n' "$line" ;;
      esac
    done <<< "$out"
  done
  printf '\n'
  info "doctor: $passes passed, $warns warning(s), $fails failure(s)"
  if [ "$fails" -gt 0 ]; then
    error "substrate wiring is broken — fix the failures above (often: re-run bootstrap.sh)"
    return 1
  fi
  ok "substrate wiring verified"
}

# ------------------------------------------------------- fmt / lint / test --

sub_fmt() {
  load_conf
  local l rc=0
  for l in $SUBSTRATE_LANGS; do "fmt_$l" || rc=1; done
  return "$rc"
}

sub_lint() {
  load_conf
  local l rc=0
  for l in $SUBSTRATE_LANGS; do "lint_$l" || rc=1; done
  return "$rc"
}

sub_test() {
  load_conf
  local l rc=0
  for l in $SUBSTRATE_LANGS; do "test_$l" || rc=1; done
  return "$rc"
}

_python() {
  # prefer project venv binaries when present
  if [ -x ".venv/bin/$1" ]; then printf '.venv/bin/%s' "$1"; else printf '%s' "$1"; fi
}

fmt_python() {
  if "$(_python ruff)" --version >/dev/null 2>&1; then
    "$(_python ruff)" format .
  elif have black; then
    black .
  else
    info "python fmt: no formatter found (ruff or black recommended)"
  fi
}

lint_python() {
  if "$(_python ruff)" --version >/dev/null 2>&1; then
    "$(_python ruff)" check .
  elif have flake8; then
    flake8 .
  else
    info "python lint: no linter found (ruff recommended)"
  fi
}

test_python() {
  if "$(_python pytest)" --version >/dev/null 2>&1; then
    "$(_python pytest)"
  elif [ -d tests ] || compgen -G 'test_*.py' >/dev/null 2>&1; then
    "$(_python python3)" -m unittest discover -v
  else
    info "python test: no tests found"
  fi
}

fmt_node()  { npm run format --if-present; }
lint_node() { npm run lint --if-present; }
test_node() { npm run test --if-present; }

fmt_go()  { gofmt -l -w .; }
lint_go() { go vet ./...; }
test_go() { go test ./...; }

fmt_rust()  { if cargo fmt --version >/dev/null 2>&1; then cargo fmt; else info "rust fmt: rustfmt not installed"; fi; }
lint_rust() { if cargo clippy --version >/dev/null 2>&1; then cargo clippy --quiet; else info "rust lint: clippy not installed"; fi; }
test_rust() { cargo test --quiet; }

fmt_java()  { info "java fmt: configure your formatter in manage.sh's place — see AGENTS.md"; }
lint_java() { info "java lint: not configured"; }
test_java() {
  if [ -x ./mvnw ]; then ./mvnw -q test
  elif [ -x ./gradlew ]; then ./gradlew test
  else info "java test: no mvnw/gradlew wrapper found"; fi
}

fmt_ruby()  { if have rubocop; then rubocop -A; else info "ruby fmt: rubocop not installed"; fi; }
lint_ruby() { if have rubocop; then rubocop; else info "ruby lint: rubocop not installed"; fi; }
test_ruby() { if [ -f Rakefile ] && have rake; then rake test; else info "ruby test: no Rakefile/rake"; fi; }

fmt_shell() { if have shfmt; then shfmt -w .; else info "shell fmt: shfmt not installed"; fi; }
lint_shell() {
  if have shellcheck; then
    local -a files=()
    while IFS= read -r -d '' f; do files+=("$f"); done \
      < <(find . -path ./.git -prune -o -name '*.sh' -type f -print0)
    if [ "${#files[@]}" -gt 0 ]; then
      shellcheck "${files[@]}"
    else
      info "shell lint: no .sh files"
    fi
  else
    info "shell lint: shellcheck not installed"
  fi
}
test_shell() { info "shell test: no runner configured (bats recommended)"; }

fmt_generic()  { info "generic fmt: nothing configured"; }
lint_generic() { info "generic lint: nothing configured"; }
test_generic() { info "generic test: nothing configured"; }

# ------------------------------------------------------------------- misc --

sub_info() {
  load_conf
  echo "project        : $(basename "$SUBSTRATE_ROOT")"
  echo "substrate root : $SUBSTRATE_ROOT"
  echo "kit version    : $SUBSTRATE_KIT_VERSION"
  echo "profile        : $SUBSTRATE_PROFILE"
  echo "languages      : $SUBSTRATE_LANGS (primary: $SUBSTRATE_PRIMARY_LANG)"
  echo "git hooks      : $([ "${SUBSTRATE_HOOKS:-0}" = "1" ] && echo wired || echo "not managed")"
  echo "kit source     : $SUBSTRATE_KIT_SOURCE"
  echo "installed at   : $SUBSTRATE_INSTALLED_AT"
  if [ -f "$SUBSTRATE_DIR/state/setup-done" ]; then
    echo "setup          : $(sed -n 's/^completed_at=//p' "$SUBSTRATE_DIR/state/setup-done" | head -1)"
  else
    echo "setup          : never run (./manage.sh setup)"
  fi
}

sub_update() {
  load_conf
  if [ -z "${SUBSTRATE_KIT_SOURCE:-}" ] || [ ! -f "$SUBSTRATE_KIT_SOURCE/bootstrap.sh" ]; then
    die "kit source not found at '${SUBSTRATE_KIT_SOURCE:-}' — run the kit's bootstrap.sh manually"
  fi
  exec bash "$SUBSTRATE_KIT_SOURCE/bootstrap.sh" \
    --profile "$SUBSTRATE_PROFILE" \
    --lang "$(printf '%s' "$SUBSTRATE_LANGS" | tr ' ' ',')"
}

sub_precommit() {
  load_conf
  if [ "${SUBSTRATE_STRICT_HOOKS:-0}" = "1" ]; then
    info "pre-commit: lint (strict)"
    sub_lint
  else
    info "pre-commit: lint (soft — set SUBSTRATE_STRICT_HOOKS=1 to enforce)"
    sub_lint || warn "lint reported issues; commit allowed anyway"
  fi
}
