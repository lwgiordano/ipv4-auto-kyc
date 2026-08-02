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


def test_documented_restore_argv_parses_and_requires_the_integrity_digest():
    """Re-audit R3-F8: the documented argv must parse through the REAL parser, and the MANDATORY
    --expect-manifest-digest cannot be omitted (the plan's example omitted it and exited argparse 2)."""
    import pytest as _pytest

    from kyc_tool.ops.restore_pr7b_core_callback import build_parser

    parser = build_parser()
    ns = parser.parse_args(
        ["--evidence", "f.json", "--expect-original-id", "7", "--expect-manifest-digest", "0" * 64]
    )
    assert ns.expect_original_id == 7 and ns.expect_manifest_digest == "0" * 64
    with _pytest.raises(SystemExit):  # omitting the mandatory integrity digest exits argparse 2
        parser.parse_args(["--evidence", "f.json", "--expect-original-id", "7"])


def test_every_documented_restore_command_includes_the_mandatory_digest_flag():
    """Re-audit R3-F8: no marked surface (module help, RUNBOOK, DEPLOYMENT, plan) may show a restore
    invocation missing --expect-manifest-digest — copying it would exit argparse 2."""
    from kyc_tool.config import REPO_ROOT

    surfaces = [
        REPO_ROOT / "src/kyc_tool/ops/restore_pr7b_core_callback.py",
        REPO_ROOT / "docs/DEPLOYMENT.md",
        REPO_ROOT / "docs/RUNBOOK.md",
        REPO_ROOT / ".agents/superpowers/plans/2026-07-23-pr7b-core-outbox-stream-separation.md",
    ]
    needle = "python -m kyc_tool.ops.restore_pr7b_core_callback"
    for path in surfaces:
        body = path.read_text()
        i = body.find(needle)
        while i != -1:
            assert "--expect-manifest-digest" in body[i : i + 400], (
                f"{path.name}: a documented restore command omits --expect-manifest-digest"
            )
            i = body.find(needle, i + 1)
