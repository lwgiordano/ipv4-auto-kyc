#!/usr/bin/env bash
# agent-substrate-kit — end-to-end test suite.
#
# Installs a copy of the kit into a temp sandbox (mirroring the documented
# ~/ai-kits/agent-substrate-kit location), builds sample projects, and drives
# the documented flow: bootstrap.sh -> ./manage.sh setup -> ./manage.sh doctor.
#
# Usage: bash tests/run-tests.sh
#
# shellcheck disable=SC2015  # `check && pass || fail` is safe here: pass/fail never fail
set -euo pipefail

KIT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd -P)"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/substrate-kit-tests.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

KIT="$SANDBOX/ai-kits/agent-substrate-kit"
mkdir -p "$KIT"
cp -R "$KIT_REPO/bootstrap.sh" "$KIT_REPO/VERSION" "$KIT_REPO/lib" "$KIT_REPO/profiles" "$KIT_REPO/templates" "$KIT/"

PASS=0
FAIL=0
CURRENT=""

t()        { CURRENT="$1"; printf '\n--- %s\n' "$1"; }
pass()     { PASS=$((PASS + 1)); printf 'ok   - %s: %s\n' "$CURRENT" "$1"; }
fail()     { FAIL=$((FAIL + 1)); printf 'FAIL - %s: %s\n' "$CURRENT" "$1"; }

# assert_ok <desc> <cmd...>   — command must exit 0
assert_ok() {
  local desc="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then pass "$desc"; else
    fail "$desc (exit $?)"
    printf '%s\n' "$out" | sed 's/^/       | /'
  fi
}

# assert_fail <desc> <cmd...> — command must exit non-zero
assert_fail() {
  local desc="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then
    fail "$desc (unexpectedly succeeded)"
    printf '%s\n' "$out" | sed 's/^/       | /'
  else pass "$desc"; fi
}

assert_file()    { [ -e "$1" ] && pass "exists: $1" || fail "missing: $1"; }
assert_no_file() { [ ! -e "$1" ] && pass "absent: $1" || fail "unexpectedly exists: $1"; }
assert_grep() {
  local pattern="$1" file="$2"
  if grep -q "$pattern" "$file" 2>/dev/null; then pass "grep '$pattern' in $file"
  else fail "no '$pattern' in $file"; fi
}

new_repo() {
  local dir="$1"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" config user.email kit-test@example.com
  git -C "$dir" config user.name "kit test"
  echo "$dir"
}

# --------------------------------------------------------------------------
t "bootstrap refuses to run outside a git repository"
NOGIT="$SANDBOX/not-a-repo"
mkdir -p "$NOGIT"
assert_fail "bootstrap fails outside git" \
  env -C "$NOGIT" GIT_CEILING_DIRECTORIES="$SANDBOX" bash "$KIT/bootstrap.sh"

# --------------------------------------------------------------------------
t "python project: bootstrap (defaults) -> setup -> doctor"
PY="$(new_repo "$SANDBOX/projects/py-app")"
cat > "$PY/requirements.txt" <<'EOF'
# no external dependencies
EOF
cat > "$PY/app.py" <<'EOF'
def greet(name: str) -> str:
    return f"hello, {name}"
EOF
cat > "$PY/test_app.py" <<'EOF'
import unittest
from app import greet


class TestGreet(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("world"), "hello, world")


if __name__ == "__main__":
    unittest.main()
EOF

assert_ok "bootstrap succeeds" env -C "$PY" bash "$KIT/bootstrap.sh"
assert_file "$PY/manage.sh"
assert_file "$PY/.substrate/lib.sh"
assert_file "$PY/.substrate/substrate.conf"
assert_file "$PY/.substrate/manifest"
assert_file "$PY/.substrate/hooks/pre-commit"
assert_file "$PY/AGENTS.md"
assert_file "$PY/CLAUDE.md"
assert_file "$PY/.editorconfig"
assert_no_file "$PY/.claude/settings.json"   # full-profile only
assert_grep 'SUBSTRATE_PROFILE="standard"' "$PY/.substrate/substrate.conf"
assert_grep 'SUBSTRATE_LANGS="python"' "$PY/.substrate/substrate.conf"
[ "$(git -C "$PY" config --local core.hooksPath)" = ".substrate/hooks" ] \
  && pass "core.hooksPath wired" || fail "core.hooksPath not wired"

assert_ok "manage.sh setup" env -C "$PY" ./manage.sh setup
assert_file "$PY/.substrate/state/setup-done"
assert_ok "manage.sh doctor" env -C "$PY" ./manage.sh doctor
assert_ok "manage.sh test (unittest)" env -C "$PY" ./manage.sh test
assert_ok "manage.sh info" env -C "$PY" ./manage.sh info
git -C "$PY" add -A
assert_ok "commit passes through pre-commit hook" \
  env -C "$PY" git commit -qm "initial"
if [ "$(git -C "$PY" ls-files | grep -c '^\.venv/' || true)" = "0" ]; then
  pass "setup artifacts (.venv/) stay out of git"
else
  fail ".venv/ files were committed — gitignore wiring broken"
fi

# --------------------------------------------------------------------------
t "re-running bootstrap is idempotent and preserves seeded files"
echo "## my notes" >> "$PY/AGENTS.md"
assert_ok "bootstrap re-run succeeds" env -C "$PY" bash "$KIT/bootstrap.sh"
assert_grep "## my notes" "$PY/AGENTS.md"
assert_ok "doctor still green" env -C "$PY" ./manage.sh doctor

# --------------------------------------------------------------------------
t "doctor detects broken wiring and recovers"
git -C "$PY" config --local --unset core.hooksPath
if out="$(cd "$PY" && ./manage.sh doctor 2>&1)"; then
  fail "doctor should exit non-zero with hooks unwired"
else
  pass "doctor exits non-zero with hooks unwired"
fi
printf '%s' "$out" | grep -q "FAIL" && pass "doctor output contains FAIL" \
  || fail "doctor output lacks FAIL"
assert_ok "bootstrap repairs wiring" env -C "$PY" bash "$KIT/bootstrap.sh"
assert_ok "doctor green after repair" env -C "$PY" ./manage.sh doctor

# --------------------------------------------------------------------------
t "node project: full profile"
JS="$(new_repo "$SANDBOX/projects/js-app")"
cat > "$JS/package.json" <<'EOF'
{
  "name": "js-app",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "test": "node --eval \"console.log('tests ok')\""
  }
}
EOF
assert_ok "bootstrap --profile full" env -C "$JS" bash "$KIT/bootstrap.sh" --profile full
assert_grep 'SUBSTRATE_LANGS="node"' "$JS/.substrate/substrate.conf"
assert_file "$JS/.claude/settings.json"
assert_file "$JS/.github/workflows/substrate-ci.yml"
assert_ok "manage.sh setup" env -C "$JS" ./manage.sh setup
assert_ok "manage.sh doctor" env -C "$JS" ./manage.sh doctor
assert_ok "manage.sh test (npm script)" env -C "$JS" ./manage.sh test

# --------------------------------------------------------------------------
t "minimal profile on an empty repo (generic language)"
MIN="$(new_repo "$SANDBOX/projects/min-app")"
assert_ok "bootstrap --profile minimal" env -C "$MIN" bash "$KIT/bootstrap.sh" --profile minimal
assert_file "$MIN/manage.sh"
assert_no_file "$MIN/AGENTS.md"
assert_no_file "$MIN/.editorconfig"
assert_grep 'SUBSTRATE_LANGS="generic"' "$MIN/.substrate/substrate.conf"
[ -z "$(git -C "$MIN" config --local --get core.hooksPath 2>/dev/null || true)" ] \
  && pass "no hook wiring in minimal profile" || fail "minimal profile wired hooks"
assert_ok "manage.sh setup" env -C "$MIN" ./manage.sh setup
assert_ok "manage.sh doctor" env -C "$MIN" ./manage.sh doctor

# --------------------------------------------------------------------------
t "--lang override and --dry-run"
SH="$(new_repo "$SANDBOX/projects/sh-app")"
assert_ok "bootstrap --dry-run" env -C "$SH" bash "$KIT/bootstrap.sh" --dry-run
assert_no_file "$SH/manage.sh"
assert_ok "bootstrap --lang shell" env -C "$SH" bash "$KIT/bootstrap.sh" --lang shell
assert_grep 'SUBSTRATE_LANGS="shell"' "$SH/.substrate/substrate.conf"
assert_ok "manage.sh doctor" env -C "$SH" ./manage.sh doctor
assert_fail "bootstrap rejects unknown language" \
  env -C "$SH" bash "$KIT/bootstrap.sh" --lang cobol
assert_fail "bootstrap rejects unknown profile" \
  env -C "$SH" bash "$KIT/bootstrap.sh" --profile deluxe

# --------------------------------------------------------------------------
t "uninstall removes managed files, keeps seeded ones"
assert_ok "bootstrap --uninstall" env -C "$PY" bash "$KIT/bootstrap.sh" --uninstall
assert_no_file "$PY/manage.sh"
assert_no_file "$PY/.substrate"
assert_file "$PY/AGENTS.md"
[ -z "$(git -C "$PY" config --local --get core.hooksPath 2>/dev/null || true)" ] \
  && pass "core.hooksPath unset after uninstall" || fail "core.hooksPath still set"

# --------------------------------------------------------------------------
printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
