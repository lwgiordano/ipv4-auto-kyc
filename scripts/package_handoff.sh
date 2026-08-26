#!/usr/bin/env bash
# Cut the source handoff package (the tarball emailed to the integration team).
#
#   scripts/package_handoff.sh <version-label>        e.g. v0.1.2-staging
#
# Produces dist/kyc-tool-<version-label>-src.tar.gz from the CURRENT COMMIT:
# a git archive of HEAD minus internal development/audit/CI files, with the
# self-contained scripts/handoff/manage.sh swapped in for the kit-backed one,
# a stamped START-HERE.md, and the deployment guide PDF freshly generated from
# this same commit. Refuses a dirty tree so the stamp cannot lie.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

VERSION="${1:?usage: scripts/package_handoff.sh <version-label>   e.g. v0.1.2-staging}"
git diff --quiet && git diff --cached --quiet \
  || { echo "error: working tree is dirty — commit first, the package stamps its commit" >&2; exit 1; }
COMMIT="$(git rev-parse --short HEAD)"

PKG="kyc-tool-$VERSION"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

git archive --format=tar --prefix="$PKG/" HEAD | tar -x -C "$STAGE"

# Internal working files: agent/audit logs, agent instructions, CI wiring, the
# tooling kit, and these packaging helpers themselves. Nothing here is needed
# to build, run, test, or operate the tool.
(
  cd "$STAGE/$PKG"
  rm -rf AGENT_BUS.md AUDIT_FINDINGS.md AGENTS.md CLAUDE.md ai-kits \
    .agents .claude .substrate .github scripts/handoff scripts/package_handoff.sh
  rm -f START-HERE.md  # never ship a stale copy if one is ever committed
)

cp scripts/handoff/manage.sh "$STAGE/$PKG/manage.sh"
chmod +x "$STAGE/$PKG/manage.sh"
sed -e "s/__VERSION__/$VERSION/g" -e "s/__COMMIT__/$COMMIT/g" \
  scripts/handoff/START-HERE.md > "$STAGE/$PKG/START-HERE.md"

# The guide publishes from the repository (its provenance gate reads git);
# the registry owns the filename.
.venv/bin/python -m docs.generators.techcraft_deployment_guide --out-dir "$STAGE/$PKG" >/dev/null
[ -f "$STAGE/$PKG/techcraft-deployment-guide.pdf" ] \
  || { echo "error: deployment guide did not publish" >&2; exit 1; }

# The package must never carry the internal files the list above prunes.
leaked="$(cd "$STAGE/$PKG" && ls -d AGENT_BUS.md AGENTS.md .substrate .github 2>/dev/null || true)"
[ -z "$leaked" ] || { echo "error: internal files leaked into the package: $leaked" >&2; exit 1; }

mkdir -p dist
OUT="$ROOT/dist/$PKG-src.tar.gz"
tar -C "$STAGE" -czf "$OUT" "$PKG"
echo "$OUT"
