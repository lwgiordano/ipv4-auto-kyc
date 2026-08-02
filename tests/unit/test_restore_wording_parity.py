"""F13 / re-audit F9: option (a)'s honest framing must not regress by surface drift.

`--expect-manifest-digest` is an operator-supplied INTEGRITY digest, NOT verified authenticity — the
tool checks the file matches the digest, it does not verify a cryptographic signature. This is a
CLAIM-PHRASE DENYLIST (not a full contract extraction): it forbids the family of phrasings that
assert the tool authenticates/proves the backup, across every operator-facing surface (module +
CLI/refusal strings, tests, RUNBOOK, DEPLOYMENT), and it is itself mutation-tested so the denylist
demonstrably has teeth against the obvious synonyms. The honest disclaimers — which use words like
"authenticity" only to say the tool does NOT do this — are deliberately not matched.
"""

from kyc_tool.config import REPO_ROOT

# Positive CLAIMS that the tool authenticates/proves the backup — dishonest until pinned-key
# signature verification exists. Each is an assertion; none appears in an honest disclaimer such as
# "the digest's authenticity is the operator's responsibility" or "does not verify a cryptographic
# signature" or "signed-manifest verification is a deliberate future option, not yet built".
_FORBIDDEN = (
    "authenticated backup",
    "authentic file",
    "authentic run",
    "authentic invocation",
    "authenticity anchor",
    "signed-manifest value",
    "cryptographically authenticates",
    "authenticates the backup",
    "proves the backup",
    "verifies the signature",
    "signature proves",
    # re-audit `d569a15..4938840` F11 — three phrasings that bypassed the earlier list:
    "establishes the backup origin",
    "establishes the origin",
    "trusted proof",
    "proof that the backup is genuine",
    "guarantees the evidence came from",
    "signed digest guarantees",
    "guarantees the backup",
    # re-audit `8aba2df..2cee937` R3-F8 — three more that evaded the list:
    "certifies the backup provenance",
    "certifies the provenance",
    "attests that the evidence came from",
    "proof of provenance",
)  # NB: "verified signature" is deliberately NOT listed — it matches the honest negation "NOT a
#     verified signature"; the positive claim is caught by "verifies the signature" instead.

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


def _hits(text_lower: str) -> list[str]:
    return [phrase for phrase in _FORBIDDEN if phrase in text_lower]


def test_no_surface_claims_the_tool_authenticates_the_backup():
    for rel in _ALL_SURFACES:
        hits = _hits((REPO_ROOT / rel).read_text().lower())
        assert not hits, (
            f"{rel} contains dishonest authenticity-claim phrase(s) {hits}: "
            f"--expect-manifest-digest is an INTEGRITY digest, not verified authenticity"
        )


def test_the_denylist_actually_catches_authenticity_claim_synonyms():
    """The guard has teeth (re-audit F9): Codex's exact bypass example and its synonym family are all
    caught, so a new dishonest phrasing cannot slip through as it did with a 6-exact-phrase list."""
    dishonest = [
        "the digest cryptographically authenticates the backup",
        "this value authenticates the backup manifest",
        "pass the authenticated backup digest",
        "the tool verifies the signature over the file",
        "the manifest proves the backup is genuine and the signature proves origin",
        "an authentic file, verified against the signed-manifest value",
        # the three phrasings that bypassed the earlier list (re-audit F11)
        "the digest establishes the backup origin",
        "a trusted proof that the backup is genuine",
        "a signed digest guarantees the evidence came from the authoritative source",
        # three more that evaded it (re-audit R3-F8)
        "the checksum certifies the backup provenance",
        "sha256 attests that the evidence came from the authoritative archive",
        "the checksum is proof of provenance",
    ]
    for sentence in dishonest:
        assert _hits(sentence.lower()), f"denylist failed to catch a dishonest claim: {sentence!r}"

    # ...and it does NOT flag the honest disclaimers (which use the same word stems in the negative).
    honest = [
        "the digest's authenticity is the operator's responsibility",
        "the tool does not verify a cryptographic signature",
        "machine-verified authenticity is a deliberate future option, not built",
        "an operator-supplied integrity digest",
    ]
    for sentence in honest:
        assert not _hits(sentence.lower()), f"denylist wrongly flagged an honest disclaimer: {sentence!r}"


def test_the_structured_integrity_contract_is_the_source_of_truth():
    """Re-audit `d569a15..4938840` F11: a machine-readable contract (not just prose/denylist) states
    exactly what --expect-manifest-digest is. Prose and the denylist must agree with it; docs and any
    rendered help assert against THIS, so a wording drift cannot silently re-scope the feature."""
    from kyc_tool.ops.restore_pr7b_core_callback import INTEGRITY_CONTRACT

    assert INTEGRITY_CONTRACT == {
        "integrity_only": True,
        "signature_verified": False,
        "authenticity": "operator_attested",
    }


def test_the_digest_is_framed_as_integrity_not_signature_verification():
    for rel in _DIGEST_DESCRIBING_SURFACES:
        text = (REPO_ROOT / rel).read_text().lower()
        assert "integrity" in text, f"{rel} must frame --expect-manifest-digest as an INTEGRITY check"
    # the module must explicitly disclaim signature verification so the limit is stated at the source
    module = (REPO_ROOT / "src/kyc_tool/ops/restore_pr7b_core_callback.py").read_text().lower()
    assert "does not" in module and "signature" in module, (
        "the restore module must explicitly state it does NOT verify a signature"
    )


def test_cli_help_is_rendered_from_the_integrity_contract():
    """Re-audit `8aba2df..2cee937` R3-F8: the CLI help DERIVES its trust semantics from
    INTEGRITY_CONTRACT, so the contract is a real source of truth consumed by help — not a dict only a
    test compares to a second literal."""
    from kyc_tool.ops.restore_pr7b_core_callback import INTEGRITY_CONTRACT, build_parser

    help_text = build_parser().format_help()
    assert f"integrity_only={INTEGRITY_CONTRACT['integrity_only']}" in help_text
    assert f"signature_verified={INTEGRITY_CONTRACT['signature_verified']}" in help_text
    assert f"authenticity={INTEGRITY_CONTRACT['authenticity']}" in help_text


# The documented-command parse proofs moved to test_restore_cli_contract.py, which EXTRACTS the
# actual commands from every surface and parses them through the real build_parser() — replacing the
# earlier hand-coded argv specimen and the 400-char substring guard (re-audit `5b0f0b8..b75a320`
# R4-F6: the substring guard stayed green when a real command was changed to
# --expect-manifest-digest-bogus).
