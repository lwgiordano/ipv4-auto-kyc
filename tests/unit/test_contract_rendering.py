"""Proof that the rendered PDFs display what the registries hold.

The authority tests prove each claim matches the running code. They cannot prove a reader ever
sees it: a claim can be correct, validated, and simply never rendered. So this file BUILDS both
PDFs and extracts their text, then asserts every required claim reached a flowable exactly once
and that its value is present on the page.

The mutation block at the end runs the SAME verifier against deliberately broken inputs. Each case
is a bypass Codex demonstrated against the previous string-matching guard, and each must fail here
(re-audit `82636da..9ac574f` F9). A guard that cannot be made to fail is not evidence.
"""

import hashlib
from collections import Counter

import pytest

pytest.importorskip("reportlab", reason="PDF renderers require reportlab")
pytest.importorskip("pdfplumber", reason="PDF text extraction requires pdfplumber")

import pdfplumber  # noqa: E402
from docs.contracts import Claim, Registry  # noqa: E402
from docs.contracts.operations import OPERATIONS  # noqa: E402
from docs.contracts.signing_example import published_snippet  # noqa: E402
from docs.contracts.wire import WIRE  # noqa: E402
from docs.generators import techcraft_deployment_guide as deploy_gen  # noqa: E402
from docs.generators import techcraft_integration_contract as contract_gen  # noqa: E402
from docs.generators.render import Doc  # noqa: E402


def _render_text(generator, tmp_path) -> str:
    doc = generator.build()
    path = str(tmp_path / "out.pdf")
    doc.build(path, "test")
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


@pytest.fixture(scope="module")
def contract_text(tmp_path_factory):
    return _render_text(contract_gen, tmp_path_factory.mktemp("contract"))


@pytest.fixture(scope="module")
def deploy_text(tmp_path_factory):
    return _render_text(deploy_gen, tmp_path_factory.mktemp("deploy"))


def _flat(text: str) -> str:
    """PDF text wraps at the column, so a claim's sentence arrives with newlines inside it."""
    return " ".join(text.split())


def _squash(text: str) -> str:
    """All whitespace removed. The signed body is 163 characters with almost no break
    opportunities, so reportlab wraps it mid-token and no space-preserving comparison can match
    it. The reader faces the same thing, which is exactly why the document publishes the byte
    count and the sha256 next to it: those let them verify a transcription this test can only
    prove was fully present."""
    return "".join(text.split())


# ── coverage: every required claim reaches a flowable exactly once ────────────────────────────────
@pytest.mark.parametrize(
    ("generator", "required"),
    [(contract_gen, contract_gen.REQUIRED_CLAIMS), (deploy_gen, deploy_gen.REQUIRED_CLAIMS)],
)
def test_every_required_claim_is_rendered_exactly_once(generator, required):
    counts = Counter(generator.build().rendered)
    missing = [c for c in required if counts[c] == 0]
    duplicated = [c for c in required if counts[c] > 1]
    assert not missing, f"required claims never rendered: {missing}"
    assert not duplicated, f"claims rendered more than once (which value is authoritative?): {duplicated}"


@pytest.mark.parametrize(
    ("generator", "registry"), [(contract_gen, WIRE), (deploy_gen, OPERATIONS)]
)
def test_renderers_only_reference_real_claims(generator, registry):
    assert set(generator.build().rendered) <= registry.ids
    assert set(generator.REQUIRED_CLAIMS) <= registry.ids


def test_every_registry_claim_is_rendered_somewhere():
    """A claim nobody renders is either dead weight or a fact we meant to publish and dropped."""
    rendered = set(contract_gen.build().rendered) | set(deploy_gen.build().rendered)
    unrendered = (WIRE.ids | OPERATIONS.ids) - rendered
    assert not unrendered, f"registry claims that reach no document: {sorted(unrendered)}"


# ── the values really are on the page ─────────────────────────────────────────────────────────────
def test_signature_vector_is_printed_whole_and_consistent(contract_text):
    v = WIRE.value("WIRE.SIGN.VECTOR")
    flat = _flat(contract_text)
    assert v["signature"] in flat
    assert v["body_sha256"] in flat
    assert v["secret"] in flat and v["key_id"] in flat and v["timestamp"] in flat
    assert f"{v['body_bytes']} bytes" in flat
    for line in v["canonical_lines"]:
        assert line in flat, f"canonical line missing from the page: {line}"
    # the printed body must hash to the printed hash: a stale pair is the bug this catches
    assert _squash(v["body"].decode()) in _squash(contract_text)
    assert hashlib.sha256(v["body"]).hexdigest() == v["body_sha256"]
    # and the reader must be told how to check their own copy, since the line wraps in print
    assert "wraps in print" in flat and "changes the digest" in flat


def test_executable_snippet_is_the_embedded_snippet(contract_text):
    flat = _flat(contract_text)
    for line in published_snippet().splitlines():
        stripped = " ".join(line.split())
        if stripped and not stripped.startswith("#"):
            assert stripped in flat, f"snippet line not embedded: {stripped!r}"


def test_direction_tokens_survive_escaping(contract_text):
    """`platform->tool` passes through reportlab's markup parser, where an unescaped `>` would be
    swallowed. Extraction proves the reader sees the literal the signer needs."""
    directions = WIRE.value("WIRE.SIGN.DIRECTIONS")
    assert directions["platform_to_tool"] in contract_text
    assert directions["tool_to_platform"] in contract_text


def test_event_table_rows_are_all_on_the_page(contract_text):
    flat = _flat(contract_text)
    for name, required, _optional, _note in WIRE.value("WIRE.EVENT.TABLE"):
        assert name in flat, f"{name} missing from the rendered table"
        for field in required:
            assert field in flat, f"{name} required field {field} not shown"


def test_status_codes_and_their_meanings_render(contract_text):
    flat = _flat(contract_text)
    for code, meaning in WIRE.value("WIRE.INGEST.STATUS"):
        assert str(code) in flat
        assert _flat(meaning)[:40] in flat, f"meaning for {code} not shown"


def test_receiver_transaction_steps_render_in_order(contract_text):
    flat = _flat(contract_text)
    positions = [flat.index(_flat(step)[:45]) for step in WIRE.value("WIRE.CALLBACK.RECEIVER_TXN")]
    assert positions == sorted(positions), "receiver steps are out of order on the page"


def test_blocker_renders_before_any_provisioning_instruction(deploy_text):
    """Placement is the claim: a production blocker after the infrastructure table is decoration."""
    flat = _flat(deploy_text)
    blocker = flat.index("This tool cannot run in production yet")
    provisioning = flat.index("Infrastructure")
    assert blocker < provisioning, "the blocker appears after the provisioning section"
    for line in OPERATIONS.value("OPS.BLOCKER.PRODUCTION_PROVIDERS"):
        assert _flat(line)[:50] in flat


@pytest.mark.parametrize("claim_id", [
    "OPS.CUTOVER.FULL_WINDOW_STEPS", "OPS.CUTOVER.BUNDLE_PINNING_STEPS",
    "OPS.CUTOVER.PR7B_PREWINDOW_STEPS", "OPS.CUTOVER.OUTBOX_CEILING",
    "OPS.HMAC.ROLLOUT_ORDER",
])
def test_cutover_steps_render_in_registry_order(deploy_text, claim_id):
    flat = _flat(deploy_text)
    positions = [flat.index(_flat(step)[:40]) for step in OPERATIONS.value(claim_id)]
    assert positions == sorted(positions), f"{claim_id} steps are out of order on the page"


def test_hmac_variable_set_renders_completely(deploy_text):
    for name in OPERATIONS.value("OPS.CONFIG.HMAC_SET"):
        assert name in deploy_text


def test_config_defaults_render_with_their_values(deploy_text):
    flat = _flat(deploy_text)
    for name, value in OPERATIONS.value("OPS.CONFIG.DEFAULTS").items():
        assert f"KYC_{name.upper()}" in flat
        assert str(value) in flat


def test_pending_claims_render_as_pending(contract_text):
    flat = _flat(contract_text)
    assert "NOT BUILT" in flat
    assert "POST-ACTIVATION" in flat
    for schema_ish in ("schema_version", "high_water_run_id", "latest_run_id"):
        assert schema_ish not in flat, "a draft 024 schema is back on the page"


# ── mutation: each bypass Codex demonstrated must fail the SAME verifier ──────────────────────────
def _doc_with(claim: Claim) -> Doc:
    doc = Doc(Registry(name="mutant", claims=(claim,)))
    doc.claim_paragraph(claim.id)
    return doc


def _rendered_text(doc: Doc, tmp_path) -> str:
    path = str(tmp_path / "mutant.pdf")
    doc.build(path, "mutant")
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def test_mutation_reordered_canonical_lines_fails_the_vector_check():
    from kyc_tool.security import canonical_v2

    v = dict(WIRE.value("WIRE.SIGN.VECTOR"))
    v["canonical_lines"] = tuple(reversed(v["canonical_lines"]))
    fields = {k: v[k] for k in
              ("key_id", "direction", "method", "path_qs", "timestamp", "slot", "body")}
    assert tuple(canonical_v2(**fields).split("\n")) != v["canonical_lines"]


def test_mutation_altered_body_breaks_the_bound_vector():
    from kyc_tool.security import sign_v2

    v = dict(WIRE.value("WIRE.SIGN.VECTOR"))
    tampered = v["body"].replace(b"ACME", b"ACNE")
    fields = {k: v[k] for k in
              ("key_id", "direction", "method", "path_qs", "timestamp", "slot")}
    assert sign_v2(v["secret"], body=tampered, **fields) != v["signature"]
    assert hashlib.sha256(tampered).hexdigest() != v["body_sha256"]


def test_mutation_sha1_sample_would_not_match():
    v = WIRE.value("WIRE.SIGN.VECTOR")
    assert hashlib.sha1(v["body"]).hexdigest() != v["body_sha256"]


def test_mutation_missing_required_field_fails_the_event_table_check():
    from kyc_tool.api.schemas import PAYLOAD_MODELS

    table = {row[0]: row for row in WIRE.value("WIRE.EVENT.TABLE")}
    name, required, _optional, _note = table["poc.token_verified"]
    trimmed = tuple(f for f in required if f != "token")
    model_required = tuple(sorted(
        f for f, i in PAYLOAD_MODELS[name].model_fields.items() if i.is_required()))
    assert tuple(sorted(trimmed)) != model_required


def test_mutation_unused_bait_string_does_not_satisfy_rendering(tmp_path):
    """The bypass that killed the previous guard: put the expected words in a string the document
    never renders. Because coverage is measured on RENDERED flowables and the assertion reads
    extracted PDF text, an unrendered constant cannot help."""
    doc = Doc(Registry(name="bait", claims=(
        Claim(id="X.SHOWN", value="the value a reader sees", authority="test"),
        Claim(id="X.HIDDEN", value="the bait nobody renders", authority="test"),
    )))
    doc.claim_paragraph("X.SHOWN")
    text = _rendered_text(doc, tmp_path)
    assert "the value a reader sees" in _flat(text)
    assert "the bait nobody renders" not in _flat(text)
    assert "X.HIDDEN" not in doc.rendered


def test_mutation_invented_sunset_date_has_no_authority():
    """A fabricated commitment must not be publishable. The claim states the dates are unset and
    negotiated, so any concrete date in it is the defect."""
    claim = WIRE.value("WIRE.SIGN.V1_SUNSET")
    assert "unset in our config" in claim
    assert not any(year in claim for year in ("2026-", "2027-", "2028-"))


def test_mutation_markup_wrapped_prohibition_still_renders_as_text(tmp_path):
    """`order by <font>decided_at</font>` bypassed the old six-tag normalizer. Rendering and
    extracting defeats that class outright: whatever markup wraps the words, the reader sees the
    words, and so does the extractor."""
    doc = Doc(Registry(name="markup", claims=(
        Claim(id="X.PROHIBITED", value="apply the newest decided_at per case", authority="test"),
    )))
    doc.claim_paragraph("X.PROHIBITED")
    assert "newest decided_at" in _flat(_rendered_text(doc, tmp_path))


def test_mutation_omitted_lifecycle_step_fails_ordering(deploy_text):
    """Dropping a step is caught because every step must be found on the page before ordering can
    be asserted; a missing one raises rather than silently passing."""
    steps = list(OPERATIONS.value("OPS.CUTOVER.FULL_WINDOW_STEPS"))
    flat = _flat(deploy_text)
    assert all(_flat(s)[:40] in flat for s in steps)
    with pytest.raises(ValueError):
        flat.index("a step this procedure does not contain at all")


def test_mutation_reordered_cutover_prerequisite_is_detected():
    """Attesting zero publishers before stopping them proves nothing. Order is asserted against
    the shipped canonical record, so a swap fails."""
    from kyc_tool.ops import cutover

    documented = list(OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING"))
    swapped = documented[:1] + [documented[2], documented[1]] + documented[3:]
    canonical = [line.split(". ", 1)[1]
                 for line in cutover.render_cutover(cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER)[1:]]
    assert documented == canonical
    assert swapped != canonical
