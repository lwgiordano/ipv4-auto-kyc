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

import pdfplumber
import pytest
from docs.contracts import Claim, Registry
from docs.contracts.operations import OPERATIONS
from docs.contracts.signing_example import published_snippet
from docs.contracts.wire import WIRE
from docs.generators import techcraft_deployment_guide as deploy_gen
from docs.generators import techcraft_integration_contract as contract_gen
from docs.generators.render import INCH, Doc

# reportlab and pdfplumber are imported at module scope ON PURPOSE (re-audit `6feca36..4f23f23`
# F4). These were `importorskip` guards, which meant an environment without the PDF extras ran the
# whole file green while proving nothing about either document — the failure mode is a passing CI
# run beside an unverified PDF. Both are in the dev extra; a missing one is a broken environment
# and should read as an error.

# Per-send release inputs the contract generator requires and has no defaults for. Any valid pair
# does for rendering; `test_release_inputs_*` covers the validation itself.
SAMPLE_CONTACT = "kyc-integration@ipv4.global"
SAMPLE_DUE_DATE = "2026-09-15"
_BUILD_ARGS = {
    id(contract_gen): {"contact": SAMPLE_CONTACT, "due_date": SAMPLE_DUE_DATE},
    id(deploy_gen): {},
}


def _build(generator) -> Doc:
    return generator.build(**_BUILD_ARGS[id(generator)])


def _render_text(generator, tmp_path) -> str:
    doc = _build(generator)
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
    counts = Counter(_build(generator).rendered)
    missing = [c for c in required if counts[c] == 0]
    duplicated = [c for c in required if counts[c] > 1]
    assert not missing, f"required claims never rendered: {missing}"
    assert not duplicated, f"claims rendered more than once (which value is authoritative?): {duplicated}"


@pytest.mark.parametrize(("generator", "registry"), [(contract_gen, WIRE), (deploy_gen, OPERATIONS)])
def test_renderers_only_reference_real_claims(generator, registry):
    assert set(_build(generator).rendered) <= registry.ids
    assert set(generator.REQUIRED_CLAIMS) <= registry.ids


def test_every_registry_claim_is_rendered_somewhere():
    """A claim nobody renders is either dead weight or a fact we meant to publish and dropped."""
    rendered = set(_build(contract_gen).rendered) | set(_build(deploy_gen).rendered)
    unrendered = (WIRE.ids | OPERATIONS.ids) - rendered
    assert not unrendered, f"registry claims that reach no document: {sorted(unrendered)}"


@pytest.mark.parametrize(("generator", "registry"), [(contract_gen, WIRE), (deploy_gen, OPERATIONS)])
def test_required_claims_name_every_claim_the_document_renders(generator, registry):
    """REQUIRED_CLAIMS is the list this file enforces. A claim rendered but left off it is
    published to TechCraft with no coverage assertion behind it, which is the same blind spot as
    not rendering it at all — just harder to notice."""
    rendered = set(_build(generator).rendered)
    unlisted = rendered - set(generator.REQUIRED_CLAIMS)
    assert not unlisted, f"rendered but absent from REQUIRED_CLAIMS: {sorted(unlisted)}"


# ── nothing runs off the page ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("generator", [contract_gen, deploy_gen])
def test_no_glyph_is_printed_outside_the_page_margins(generator, tmp_path):
    """Every other test in this file reads extracted text — and pdfplumber reports glyphs whose
    coordinates lie OUTSIDE the page, so text clipped at the edge extracts perfectly while being
    invisible in print. Three real defects hid behind exactly that: the receiver's
    commit-before-2xx steps, the outbox-ceiling cutover, and the 163-byte signed body all ran off
    the right edge, and every content assertion passed. Geometry is the only check that sees it.
    """
    path = str(tmp_path / "margins.pdf")
    _build(generator).build(path, "margins")

    overflowing = []
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            right = page.width - 0.75 * 72
            for char in page.chars:
                if char["x1"] > right + 1 or char["x0"] < 0.75 * 72 - 1:
                    overflowing.append((number, round(char["x0"]), round(char["x1"]), char["text"]))
    assert not overflowing, (
        f"{len(overflowing)} glyphs printed outside the text frame, first few: {overflowing[:12]}"
    )


@pytest.mark.parametrize("generator", [contract_gen, deploy_gen])
def test_no_body_text_collides_with_the_footer_or_the_page_edges(generator, tmp_path):
    """Vertical bounds. The x-only check passed while body glyphs sat under the footer.

    Re-audit `4f23f23..97deeae` finding 9: checking `x0`/`x1` only means shrinking the bottom
    margin puts body text on top of the stamped provenance line with zero reported overflow — and
    the footer is the one thing on the page that says which commit it came from.
    """
    path = str(tmp_path / "vertical.pdf")
    _build(generator).build(path, "vertical")
    offenders = []
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            footer_top = page.height - 0.62 * 72
            top_margin = 0.7 * 72
            for char in page.chars:
                if char["bottom"] > page.height - 0.3 * 72 or char["top"] < 0:
                    offenders.append((number, "below the page", char["text"]))
                elif char["top"] < top_margin - 12:
                    offenders.append((number, "above the top margin", char["text"]))
                elif char["top"] > footer_top and char["size"] > 7.2:
                    # the footer itself is 7pt; anything larger down there is body text
                    offenders.append((number, "in the footer band", char["text"]))
    assert not offenders, f"{len(offenders)} glyphs out of bounds, first: {offenders[:10]}"


@pytest.mark.parametrize("generator", [contract_gen, deploy_gen])
def test_no_two_rendered_lines_are_drawn_on_top_of_each_other(generator, tmp_path):
    """Overlap. A negative spacer prints one paragraph over another and every x/y BOUNDS check
    still passes — both flowables are inside the frame, they are just in the same place."""
    path = str(tmp_path / "overlap.pdf")
    _build(generator).build(path, "overlap")
    collisions = []
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            rows: dict[float, list] = {}
            for word in page.extract_words():
                rows.setdefault(round(word["top"], 1), []).append(word)
            boxes = sorted(
                (min(w["top"] for w in ws), max(w["bottom"] for w in ws),
                 min(w["x0"] for w in ws), max(w["x1"] for w in ws),
                 " ".join(w["text"] for w in ws)[:40])
                for ws in rows.values()
            )
            for (_top_a, bottom_a, x0_a, x1_a, text_a), (top_b, _b, x0_b, x1_b, text_b) in zip(
                    boxes, boxes[1:], strict=False):
                vertical = top_b < bottom_a - 1.0
                horizontal = x0_b < x1_a and x0_a < x1_b
                if vertical and horizontal:
                    collisions.append((number, text_a, text_b))
    assert not collisions, f"lines drawn over one another: {collisions[:6]}"


def test_the_overlap_guard_can_actually_fail(tmp_path):
    """Codex's exact mutation: a negative spacer that prints one paragraph over another."""
    from reportlab.platypus import Spacer

    doc = Doc(WIRE)
    doc.p("The first paragraph, which should be legible on its own line.")
    doc.story.append(Spacer(1, -24))
    doc.p("The second paragraph, printed straight over the top of the first.")
    path = str(tmp_path / "collide.pdf")
    doc.build(path, "collide")

    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        rows: dict[float, list] = {}
        for word in page.extract_words():
            rows.setdefault(round(word["top"], 1), []).append(word)
        boxes = sorted((min(w["top"] for w in ws), max(w["bottom"] for w in ws)) for ws in
                       rows.values())
    assert any(top_b < bottom_a - 1.0 for (_a, bottom_a), (top_b, _b) in
               zip(boxes, boxes[1:], strict=False)), (
        "the negative spacer did not produce overlapping lines, so the guard above proves nothing"
    )


def test_a_release_build_refuses_unverifiable_provenance(tmp_path, monkeypatch):
    """`source_revision()` swallows every failure, so a build host without git emitted a
    release-looking PDF stamped `source unknown` (re-audit finding 9). A preview may; a release
    may not."""
    from docs.generators import render

    doc = _build(contract_gen)
    monkeypatch.setattr(render, "source_revision", lambda: "unknown")
    with pytest.raises(render.ProvenanceError, match="source commit"):
        doc.build(str(tmp_path / "release.pdf"), "release", release=True)

    monkeypatch.setattr(render, "source_revision", lambda: "abc1234+dirty")
    with pytest.raises(render.ProvenanceError, match="uncommitted"):
        doc.build(str(tmp_path / "release.pdf"), "release", release=True)

    # a clean commit is fine, and a preview never asks
    monkeypatch.setattr(render, "source_revision", lambda: "abc1234")
    doc.build(str(tmp_path / "release.pdf"), "release", release=True)
    monkeypatch.setattr(render, "source_revision", lambda: "unknown")
    doc.build(str(tmp_path / "preview.pdf"), "preview")


def test_the_margin_guard_can_actually_fail():
    """A guard nobody can make fail is not evidence. `code()` refuses an over-wide preformatted
    line at build time, which is the same boundary the geometric test measures after the fact."""
    from docs.generators.render import _guard_preformatted

    doc = Doc(WIRE)
    with pytest.raises(ValueError, match="run off the page"):
        doc.code("x = " + "y" * 400)
    # the join that produced the run-on line: `<br/>` is dropped by XPreformatted, so passing it
    # as markup silently concatenates the lines it was meant to separate
    with pytest.raises(ValueError, match="ignores <br/>"):
        _guard_preformatted("first<br/>second")
    doc.code("first<br/>second")  # as literal text it is escaped, not markup, and is fine
    doc.wrapcode("z" * 400)  # the wrapping style takes what the preformatted one refuses


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


def _event_table_cells(path: str) -> dict[str, tuple[frozenset, frozenset]]:
    """Read the event table back out of the PDF cell by cell.

    Extracting cells rather than searching page text is the whole point (re-audit
    `6feca36..4f23f23` F10). `field in page_text` passes when the field name happens to appear in a
    neighbouring row's Notes column, or in a paragraph three pages earlier — so a field dropped
    from a row, or moved from required to optional, went unnoticed. Cells bind each token to the
    row and column it was published in.
    """
    tables: dict[str, tuple[frozenset, frozenset]] = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                header = [(cell or "").strip() for cell in table[0]]
                if header[:1] != ["event_type"]:
                    continue
                for row in table[1:]:
                    name = " ".join((row[0] or "").split())
                    tables[name] = (_tokens(row[1]), _tokens(row[2]))
    return tables


def _tokens(cell: str | None) -> frozenset:
    """A payload cell holds one identifier per rendered line. Split on whitespace and keep whole
    tokens: a name broken across lines by wrapping arrives here as two fragments and fails the
    set comparison, which is the failure the fixed-width comma-only-break cell exists to prevent."""
    raw = (cell or "").replace(",", " ").split()
    return frozenset(t for t in raw if t and t != "(none)")


def test_event_table_publishes_every_field_in_the_right_cell(tmp_path):
    """Every required AND optional field, per row, as exact whole tokens. Optional fields matter
    as much as required ones here: a caller who never learns a field is accepted cannot send it,
    and payloads allow extras, so a misspelling 202s while populating nothing."""
    path = str(tmp_path / "cells.pdf")
    _build(contract_gen).build(path, "cells")
    rendered = _event_table_cells(path)

    documented = {
        name: (frozenset(req), frozenset(opt)) for name, req, opt, _note in WIRE.value("WIRE.EVENT.TABLE")
    }
    assert set(rendered) == set(documented), (
        f"rows on the page do not match the registry: "
        f"missing {sorted(set(documented) - set(rendered))}, "
        f"unexpected {sorted(set(rendered) - set(documented))}"
    )
    for name, (required, optional) in documented.items():
        page_required, page_optional = rendered[name]
        assert page_required == required, f"{name} required cell: {page_required} != {required}"
        assert page_optional == optional, f"{name} optional cell: {page_optional} != {optional}"


def test_a_token_too_wide_for_its_column_fails_the_build(tmp_path):
    """Found by the cell check above: `website.review_completed` printed as
    `website.review_complete`, one character short and 422-on-arrival, because a no-wrap cell
    CLIPS rather than wraps. Silent truncation of an identifier is the worst outcome a document
    can produce, so it now raises at build time."""
    doc = Doc(
        Registry(
            name="narrow",
            claims=(Claim(id="X.WIDE", value=(("website.review_completed", "note"),), authority="test"),),
        )
    )
    with pytest.raises(ValueError, match="would be clipped"):
        doc.claim_table("X.WIDE", ("event_type", "Notes"), [0.5 * INCH, 3 * INCH], code_columns=(0,))
    assert "X.WIDE" not in doc.rendered, "a failed cell still counted as rendered"

    doc.claim_table("X.WIDE", ("event_type", "Notes"), [2 * INCH, 3 * INCH], code_columns=(0,))
    path = str(tmp_path / "wide.pdf")
    doc.build(path, "wide")
    with pdfplumber.open(path) as pdf:
        assert "website.review_completed" in _flat(pdf.pages[0].extract_text() or "")


def test_a_field_split_across_lines_would_fail_the_cell_check():
    """The guard's own failure mode, exercised directly: wrapping `platform_account_id` into two
    fragments must not compare equal to the whole token."""
    assert _tokens("platform\n_account_id") != frozenset({"platform_account_id"})
    assert _tokens("platform_account_id\nregistry_country") == frozenset(
        {"platform_account_id", "registry_country"}
    )


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


@pytest.mark.parametrize("claim_id", ["OPS.CUTOVER.OUTBOX_CEILING", "OPS.HMAC.ROLLOUT_ORDER"])
def test_cutover_steps_render_in_registry_order(deploy_text, claim_id):
    flat = _flat(deploy_text)
    positions = [flat.index(_flat(step)[:40]) for step in OPERATIONS.value(claim_id)]
    assert positions == sorted(positions), f"{claim_id} steps are out of order on the page"


def test_each_cutover_procedure_renders_its_prerequisites_and_its_playbook(deploy_text):
    """These claims replaced three step summaries with pointers, so the pointer and the
    prerequisites are now the entire published content — if either is missing from the page, an
    operator has a document telling them a procedure exists and nothing else."""
    flat = _flat(deploy_text)
    for procedure in OPERATIONS.value("OPS.CUTOVER.PROCEDURES"):
        assert procedure.name in flat, f"{procedure.name} is not on the page"
        assert _flat(procedure.playbook) in flat, f"{procedure.name}: playbook pointer missing"
        assert _flat(procedure.when)[:60] in flat, f"{procedure.name}: 'when' missing"
        assert _flat(procedure.irreversible)[:60] in flat
        for prerequisite in procedure.blocks_start:
            assert _flat(prerequisite)[:60] in flat, (
                f"{procedure.name}: prerequisite not shown: {prerequisite[:60]!r}"
            )


def test_the_abort_sentinel_prints_as_one_word(deploy_text):
    """An operator greps their logs for this. Rendered in a narrow cell it came out as
    `BLOCKE` / `D_NO_AUTHORITATIVE_MAPPING`, which matches nothing."""
    assert "BLOCKED_NO_AUTHORITATIVE_MAPPING" in _flat(deploy_text)


def test_a_word_wider_than_its_column_fails_the_build():
    """The prose-cell half of the clipping guard. CELL sets splitLongWords=0 so identifiers are
    never cut in half — which means an over-wide one is clipped instead, so it must raise."""
    doc = Doc(Registry(name="narrowprose", claims=(
        Claim(id="X.LONG", value=(("BLOCKED_NO_AUTHORITATIVE_MAPPING", "note"),), authority="t"),
    )))
    with pytest.raises(ValueError, match="would be\n?\\s*clipped"):
        doc.claim_table("X.LONG", ("Term", "Notes"), [0.7 * INCH, 3 * INCH])
    assert "X.LONG" not in doc.rendered
    doc.claim_table("X.LONG", ("Term", "Notes"), [3 * INCH, 3 * INCH])


def test_the_deployment_guide_publishes_no_cutover_step_numbering(deploy_text):
    """The removal is the point (operations.py, F5 note). A numbered procedure reappearing here
    means the drift-prone second copy is back."""
    flat = _flat(deploy_text)
    for banned in ("1. Stop the API pool", "1. Deploy the rolling part", "1. Suspend the retention"):
        assert banned not in flat, f"a cutover step summary is back on the page: {banned!r}"


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
    fields = {k: v[k] for k in ("key_id", "direction", "method", "path_qs", "timestamp", "slot", "body")}
    assert tuple(canonical_v2(**fields).split("\n")) != v["canonical_lines"]


def test_mutation_altered_body_breaks_the_bound_vector():
    from kyc_tool.security import sign_v2

    v = dict(WIRE.value("WIRE.SIGN.VECTOR"))
    tampered = v["body"].replace(b"ACME", b"ACNE")
    fields = {k: v[k] for k in ("key_id", "direction", "method", "path_qs", "timestamp", "slot")}
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
    model_required = tuple(sorted(f for f, i in PAYLOAD_MODELS[name].model_fields.items() if i.is_required()))
    assert tuple(sorted(trimmed)) != model_required


def test_mutation_unused_bait_string_does_not_satisfy_rendering(tmp_path):
    """The bypass that killed the previous guard: put the expected words in a string the document
    never renders. Because coverage is measured on RENDERED flowables and the assertion reads
    extracted PDF text, an unrendered constant cannot help."""
    doc = Doc(
        Registry(
            name="bait",
            claims=(
                Claim(id="X.SHOWN", value="the value a reader sees", authority="test"),
                Claim(id="X.HIDDEN", value="the bait nobody renders", authority="test"),
            ),
        )
    )
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
    doc = Doc(
        Registry(
            name="markup",
            claims=(
                Claim(id="X.PROHIBITED", value="apply the newest decided_at per case", authority="test"),
            ),
        )
    )
    doc.claim_paragraph("X.PROHIBITED")
    assert "newest decided_at" in _flat(_rendered_text(doc, tmp_path))


def test_mutation_omitted_lifecycle_step_fails_ordering(deploy_text):
    """Dropping a step is caught because every step must be found on the page before ordering can
    be asserted; a missing one raises rather than silently passing."""
    steps = list(OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING"))
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
    canonical = [
        line.split(". ", 1)[1] for line in cutover.render_cutover(cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER)[1:]
    ]
    assert documented == canonical
    assert swapped != canonical


# ── re-audit `6feca36..4f23f23` F3: the published signer must actually be copyable ────────────────
def test_published_slice_compiles_and_runs_in_an_empty_namespace():
    """A reader copies exactly what the markers enclose and nothing else. Executing it must define
    a working `sign` — the previous slice began AFTER `import hashlib, hmac`, so it raised
    NameError on first call."""
    namespace: dict = {}
    exec(compile(published_snippet(), "<published-snippet>", "exec"), namespace)  # noqa: S102
    v = WIRE.value("WIRE.SIGN.VECTOR")
    produced = namespace["sign"](
        v["secret"],
        key_id=v["key_id"],
        direction=v["direction"],
        method=v["method"],
        path_qs=v["path_qs"],
        timestamp=v["timestamp"],
        slot=v["slot"],
        body=v["body"],
    )
    assert produced == v["signature"]


def test_the_signer_block_is_whole_and_on_one_page(tmp_path):
    """The printed block is an ILLUSTRATION, and it still has to be legible in one piece.

    What this test no longer claims is that the block is copyable. The previous version
    reconstructed indentation from glyph x-offsets and Courier advance widths and then compiled
    the result — proving the glyphs contain enough information for a custom decoder, not that a
    reader with a normal PDF viewer can use it (re-audit `4f23f23..122cc67` finding 4). Exact
    `pdftotext` output does not compile. The runnable copy is the companion file, checked by
    `test_the_companion_file_is_the_bytes_the_document_names`.
    """
    from docs.contracts.signing_example import COPY_BEGIN, COPY_END

    path = str(tmp_path / "signer.pdf")
    _build(contract_gen).build(path, "signer")

    with pdfplumber.open(path) as pdf:
        pages = [(number, page.extract_text() or "") for number, page in enumerate(pdf.pages, 1)]
    holding = [n for n, text in pages if COPY_BEGIN in text]
    closing = [n for n, text in pages if COPY_END in text]
    assert len(holding) == 1 and holding == closing, (
        f"the signer block spans pages {holding} to {closing}; page furniture would land inside it"
    )
    block_page = dict(pages)[holding[0]]
    for line in published_snippet().splitlines():
        stripped = " ".join(line.split())
        if stripped and not stripped.startswith("#"):
            assert stripped in _flat(block_page), f"snippet line missing from the page: {stripped!r}"


def test_the_document_does_not_promise_the_page_is_copyable(contract_text):
    """A document that says "copy from here" and cannot be copied from is worse than one that
    points at a file."""
    flat = _flat(contract_text)
    from docs.contracts.companion import ARTIFACT_NAME, artifact_digest

    assert ARTIFACT_NAME in flat
    assert artifact_digest() in flat
    assert "not supported" in flat


def test_code_on_the_page_keeps_its_indentation(tmp_path):
    """Paragraph collapsed leading whitespace, so the printed Python could not compile when copied
    off the page. Rendered code must preserve the x-offset of nested lines."""
    from docs.generators.render import Doc as _Doc

    doc = _Doc(WIRE)
    doc.code("def outer():\n    nested = 1\n    return nested")
    path = str(tmp_path / "indent.pdf")
    doc.build(path, "indent")
    with pdfplumber.open(path) as pdf:
        words = pdf.pages[0].extract_words()
    offsets = {w["text"]: w["x0"] for w in words}
    assert offsets["nested"] > offsets["def"], "nested code lost its indentation on the page"


# ── re-audit `6feca36..4f23f23` F11: a document that asks for a reply must name where ─────────────
@pytest.mark.parametrize(
    "contact",
    [
        "",
        "   ",
        "[integration contact - fill in]",
        "fill in",
        "TBD",
        "integration team",  # no address and no URL: nowhere to send anything
    ],
)
def test_release_inputs_refuse_an_unusable_contact(contact):
    with pytest.raises(ValueError):
        contract_gen.validate_release_inputs(contact, SAMPLE_DUE_DATE)


@pytest.mark.parametrize("due", ["", "   ", "18 August", "2026-13-01", "2026/09/15", "next Friday"])
def test_release_inputs_refuse_an_unusable_due_date(due):
    with pytest.raises(ValueError):
        contract_gen.validate_release_inputs(SAMPLE_CONTACT, due)


def test_the_contract_cannot_be_generated_without_both(capsys):
    """argparse exits non-zero rather than emitting a document with a placeholder in it."""
    for argv in ([], ["--integration-contact", SAMPLE_CONTACT], ["--response-due-date", SAMPLE_DUE_DATE]):
        with pytest.raises(SystemExit) as excinfo:
            contract_gen.main(argv)
        assert excinfo.value.code != 0
    capsys.readouterr()


def test_the_reply_channel_and_deadline_reach_the_page(contract_text):
    flat = _flat(contract_text)
    assert SAMPLE_CONTACT in flat, "the document asks for answers and names no contact"
    assert SAMPLE_DUE_DATE in flat, "the document asks for answers and names no deadline"
    for placeholder in ("fill in", "TBD", "[integration contact"):
        assert placeholder not in flat


def test_page_indentation_structure_matches_the_compilable_source(tmp_path):
    """The page must carry the SAME indentation structure as the source that compiles.

    Asserted on glyph x-offsets rather than by compiling extracted text: `extract_text()` drops
    leading whitespace outright, and `layout=True` quantizes columns, so compiling either output
    would be a test of pdfplumber rather than of the document. Distinct source indent levels must
    appear as distinct, correspondingly ordered x-offsets on the page."""
    path = str(tmp_path / "contract.pdf")
    _build(contract_gen).build(path, "t")

    # rebuild rendered LINES (grouped by vertical position), not loose words: the same token
    # appears elsewhere in the document, so matching by word text alone reads x-offsets from
    # unrelated paragraphs
    rendered: list[tuple[float, str]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines: dict[float, list] = {}
            for word in page.extract_words():
                lines.setdefault(round(word["top"], 1), []).append(word)
            for top in sorted(lines):
                row = sorted(lines[top], key=lambda w: w["x0"])
                rendered.append((row[0]["x0"], " ".join(w["text"] for w in row)))

    # Match each source line to the rendered line carrying its text, scanning FORWARD from the
    # previous match. Slicing a fixed-length window by index instead reads whatever follows when
    # the block straddles a page break — footer text at the body margin then masquerades as a
    # deeply indented line and the ordering assertion passes or fails for the wrong reason.
    source_lines = [ln for ln in published_snippet().splitlines() if ln.strip()]
    by_indent: dict[int, list[float]] = {}
    cursor = 0
    for source in source_lines:
        wanted = " ".join(source.split())
        match = next((i for i in range(cursor, len(rendered)) if rendered[i][1] == wanted), None)
        assert match is not None, f"snippet line not found on the page: {wanted!r}"
        cursor = match + 1
        by_indent.setdefault(len(source) - len(source.lstrip()), []).append(rendered[match][0])

    levels = sorted(by_indent)
    assert len(levels) >= 2, "the snippet has no nesting to prove"
    for shallower, deeper in zip(levels, levels[1:], strict=False):
        assert min(by_indent[deeper]) > min(by_indent[shallower]), (
            f"source indent {deeper} does not render right of indent {shallower}"
        )


# ── overlap, measured in the LAYOUT, not on the page ─────────────────────────────────────────────
def _sibling_overlaps(doc) -> list[tuple]:
    """Pairs of flowables whose final rectangles intersect.

    Container flowables legitimately contain their children — a KeepTogether's rectangle covers
    the paragraphs inside it — so only SIBLING rectangles on the same page are compared, and a
    rectangle wholly containing another is treated as containment rather than collision.
    """
    collisions = []
    by_page: dict[int, list] = {}
    for placement in doc.placements:
        if placement["what"] in ("LCActionFlowable", "ActionFlowable", "Spacer"):
            continue
        by_page.setdefault(placement["page"], []).append(placement)
    for items in by_page.values():
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                vertical = a["bottom"] < b["top"] - 0.5 and b["bottom"] < a["top"] - 0.5
                horizontal = a["x0"] < b["x1"] - 0.5 and b["x0"] < a["x1"] - 0.5
                if not (vertical and horizontal):
                    continue
                # STRICT containment only. A KeepTogether's rectangle is strictly larger than the
                # paragraphs inside it. Two flowables with the SAME rectangle are not nested —
                # they are drawn on top of each other, which is precisely the case a
                # non-strict `<=` comparison waves through.
                contains = ((a["bottom"] < b["bottom"] and a["top"] > b["top"]) or
                            (b["bottom"] < a["bottom"] and b["top"] > a["top"]))
                if contains:
                    continue
                collisions.append((a["page"], a["text"], b["text"]))
    return collisions


@pytest.mark.parametrize("generator", [contract_gen, deploy_gen])
def test_no_two_flowables_are_laid_out_on_top_of_each_other(generator, tmp_path):
    """Re-audit `4f23f23..122cc67` finding 11. `Spacer(1, -18)` puts two paragraphs on the same
    baseline; the page is visibly interleaved, but the extractor merges their glyphs into one word
    so a line-box detector reports zero collisions. The layout engine knows the rectangles."""
    doc = _build(generator)
    doc.build(str(tmp_path / "overlap-layout.pdf"), "overlap")
    collisions = _sibling_overlaps(doc)
    assert not collisions, f"flowables laid out over one another: {collisions[:6]}"


def test_the_layout_overlap_guard_catches_the_exact_baseline_case(tmp_path):
    """The mutation the page-level check could not see, run against the layout-level one."""
    from reportlab.platypus import Spacer

    doc = Doc(WIRE)
    doc.p("The first paragraph, which should be legible on its own line.")
    doc.story.append(Spacer(1, -18))
    doc.p("The second paragraph, laid out on the same baseline as the first.")
    doc.build(str(tmp_path / "collide-layout.pdf"), "collide")

    assert _sibling_overlaps(doc), (
        "the negative spacer produced no detected overlap, so the guard above proves nothing"
    )


# ── a separated page is still locatable ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("generator", [contract_gen, deploy_gen])
def test_every_page_names_the_section_it_belongs_to(generator, tmp_path):
    """Re-audit `4f23f23..122cc67` finding 12. Pages opened mid-sentence — `Rolling back:` split
    from the procedure it belongs to, `case. Treat a 409...` with no heading — so a page read on
    its own could not be placed. Forbidding a paragraph from crossing a page boundary would be
    fighting typography; naming the section on every page fixes what the finding is actually
    about, which is that a separated page was not independently understandable."""
    path = str(tmp_path / "sections.pdf")
    doc = _build(generator)
    doc.build(path, "sections")

    with pdfplumber.open(path) as pdf:
        pages = [(n, page.extract_text() or "") for n, page in enumerate(pdf.pages, 1)]
    for number, text in pages:
        footer = _flat(text.strip().split("\n")[-1])
        assert "source" in footer and f"Page {number} of" in footer, (
            f"page {number} has no provenance footer: {footer[:80]!r}")
        if number == 1:
            continue  # the title page is self-identifying
        section = doc.page_sections.get(number, "")
        assert section, f"page {number} belongs to no section"
        assert _flat(section) in footer, (
            f"page {number} does not name its section: {footer[:100]!r}")


def test_the_section_footer_tracks_section_changes(tmp_path):
    """Guard the guard: if every page reported the same section the test above would pass while
    telling a reader nothing."""
    doc = _build(deploy_gen)
    doc.build(str(tmp_path / "track.pdf"), "track")
    assert len(set(doc.page_sections.values())) > 1, doc.page_sections


def test_f15_gate_the_effectiveness_heading_shares_a_page_with_its_table(tmp_path):
    """Gate audit `6c4f54a..91fbde3` finding 15: a fresh preview showed page 4 ending with only
    'Recording a callback is not the same as acting on it', the transition table starting
    overleaf with no repeated heading. The heading, its note, and the table now travel as one
    KeepTogether unit, proven by geometry: the heading's page must also carry the table header."""
    path = str(tmp_path / "keep.pdf")
    _build(contract_gen).build(path, "keep")
    heading = "Recording a callback is not the same as acting on it"
    with pdfplumber.open(path) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    heading_pages = [i for i, text in enumerate(pages) if heading in text]
    assert len(heading_pages) == 1
    page = pages[heading_pages[0]]
    assert "When this row applies" in page, (
        "the effectiveness heading is orphaned: its table does not start on the same page"
    )


def test_r3f6_the_built_pdf_orders_validation_before_history_and_table(tmp_path):
    """R-audit-3 finding 6, proven on the BUILT artifact: the visible order is signature
    verification -> shape/integrity validation -> transaction/history/table, and a partial or
    mismatched release binding is described as a HOLD — never as record-and-2xx."""
    path = str(tmp_path / "order.pdf")
    _build(contract_gen).build(path, "order")
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    validate_at = text.find("Validate before you classify")
    commit_at = text.find("Your endpoint must commit before it answers")
    table_at = text.find("Recording a callback is not the same as acting on it")
    assert validate_at != -1 and commit_at != -1 and table_at != -1
    assert validate_at < commit_at < table_at, (
        "the published order is no longer signature -> validation -> transaction/table"
    )
    # the partial-binding row is a HOLD, and nowhere does the document instruct recording it
    partial_at = text.find("PARTIAL release binding")
    assert partial_at != -1, "the partial-binding disposition vanished from the document"
    disposition = text[partial_at:partial_at + 220]
    assert "HOLD" in disposition and "do not record" in disposition, (
        "the partial-binding disposition no longer reads as a hold"
    )
