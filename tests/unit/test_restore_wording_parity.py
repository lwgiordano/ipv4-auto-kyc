"""F13 (`b39b82a..b53daf4`): option (a)'s honest framing must not regress by surface drift.

`--expect-manifest-digest` is an operator-supplied INTEGRITY digest, NOT verified authenticity — the
tool checks the file matches the digest, it does not verify a cryptographic signature. That framing
was corrected in the module docstring and the operator docs, but the inline comment, the refusal
string, and the test helper still called the argument a "signed-manifest value" / an "authenticated
backup" / an "authentic run". This static guard pins the honest framing across every operator-facing
surface (module + CLI/refusal strings, tests, RUNBOOK, DEPLOYMENT) so it cannot silently drift back.
"""

from kyc_tool.config import REPO_ROOT

# Phrasings that CLAIM the tool authenticates the backup — dishonest until pinned-key signature
# verification exists. These are specific misleading claims, not the honest disclaimers that use the
# word "authenticity" to say the tool does NOT do this (e.g. "the digest's authenticity is the
# operator's responsibility"), which must remain allowed.
_FORBIDDEN = (
    "authenticated backup",
    "authentic file",
    "authentic run",
    "authentic invocation",
    "authenticity anchor",
    "signed-manifest value",
)

_ALL_SURFACES = (
    "src/kyc_tool/ops/restore_pr7b_core_callback.py",
    "tests/integration/test_restore_pr7b_core_callback.py",
    "docs/RUNBOOK.md",
    "docs/DEPLOYMENT.md",
)

# Surfaces that describe what --expect-manifest-digest IS must frame it as an INTEGRITY check (the
# test module is scenario code, exempt from the positive requirement).
_DIGEST_DESCRIBING_SURFACES = (
    "src/kyc_tool/ops/restore_pr7b_core_callback.py",
    "docs/RUNBOOK.md",
    "docs/DEPLOYMENT.md",
)


def test_no_surface_claims_the_tool_authenticates_the_backup():
    for rel in _ALL_SURFACES:
        text = (REPO_ROOT / rel).read_text().lower()
        for phrase in _FORBIDDEN:
            assert phrase not in text, (
                f"{rel} contains the misleading claim {phrase!r}: --expect-manifest-digest is an "
                f"INTEGRITY digest, not verified authenticity (re-audit F13)"
            )


def test_the_digest_is_framed_as_integrity_not_signature_verification():
    for rel in _DIGEST_DESCRIBING_SURFACES:
        text = (REPO_ROOT / rel).read_text().lower()
        assert "integrity" in text, f"{rel} must frame --expect-manifest-digest as an INTEGRITY check"
    # the module must explicitly disclaim signature verification so the limit is stated at the source
    module = (REPO_ROOT / "src/kyc_tool/ops/restore_pr7b_core_callback.py").read_text().lower()
    assert "does not" in module and "signature" in module, (
        "the restore module must explicitly state it does NOT verify a signature"
    )
