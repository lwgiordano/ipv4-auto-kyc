# shellcheck shell=bash
# agent-substrate-kit — project language detection.
# Sourced by bootstrap.sh. Detection runs against the current working
# directory (the project root).

SUPPORTED_LANGS="python node go rust java ruby shell generic"

# detect_languages
# Prints the detected languages, space-separated, primary language first.
# A project can match several (e.g. a Go service with a Node frontend).
# Falls back to "generic" when nothing matches.
detect_languages() {
  local langs=()

  [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ] || [ -f requirements.txt ] \
    && langs+=(python)
  [ -f package.json ] && langs+=(node)
  [ -f go.mod ] && langs+=(go)
  [ -f Cargo.toml ] && langs+=(rust)
  [ -f pom.xml ] || [ -f build.gradle ] || [ -f build.gradle.kts ] \
    && langs+=(java)
  [ -f Gemfile ] && langs+=(ruby)

  # Shell counts only when nothing more specific matched and shell scripts
  # exist at the top level.
  if [ "${#langs[@]}" -eq 0 ]; then
    if compgen -G '*.sh' >/dev/null 2>&1 || compgen -G 'scripts/*.sh' >/dev/null 2>&1; then
      langs+=(shell)
    fi
  fi

  [ "${#langs[@]}" -eq 0 ] && langs+=(generic)
  printf '%s\n' "${langs[*]}"
}

# validate_langs <space-separated langs>
# Dies if any language is not supported.
validate_langs() {
  local l
  for l in $1; do
    case " $SUPPORTED_LANGS " in
      *" $l "*) ;;
      *) die "unsupported language '$l' (supported: $SUPPORTED_LANGS)" ;;
    esac
  done
}
