"""The built PDF is compared to a typed document model, span by span.

Re-audit `4f23f23..97deeae` F3. The previous proof was "every required claim id appears in a list
of rendered ids" plus "the claim's text appears somewhere in the page". Counting ids and searching
globally cannot prove what a reader saw, and Codex demonstrated it: the suite stayed green after

  * the visible meaning of `WIRE.ORDERING.NO_DECIDED_AT` was removed while its id stayed recorded,
  * the eight canonical signing lines were reversed on the page,
  * and a visibly contradictory, unclaimed commit-before-2xx paragraph was appended.

None of those change an id count, and the first two do not change the SET of strings on the page
either — only their presence, order, and attribution. So this file works from
`docs.generators.render.Doc`'s block model: every block records the exact lines it emits, and each
one is located on the built page, in document order, exactly once, inside its declared section.
"""

import contextlib
import dataclasses
import hashlib
import re
from collections import Counter

import pdfplumber
import pytest
from docs.contracts import PROSE, Claim, Column, ColumnRole, Registry, documents, projection
from docs.contracts.documents import TEST_FIXTURE
from docs.contracts.operations import OPERATIONS
from docs.contracts.wire import WIRE
from docs.generators import render as render_module
from docs.generators import techcraft_deployment_guide as deploy_gen
from docs.generators import techcraft_integration_contract as contract_gen
from docs.generators.render import Doc

from .test_contract_rendering import SAMPLE_CONTACT, SAMPLE_DUE_DATE, _build

# A line short enough to occur legitimately more than once ("300", "POST", a repeated cell value).
# Above it, a second occurrence is a duplicate nobody asked for — which is the mutation
# "add an unrecorded duplicate block".
UNIQUE_LINE_CHARS = 45

# Ad-hoc Docs below are FIXTURES, not published documents; they name a fixture identity that
# lives in the same closed registry as the real ones, because identity is never a caller's to
# invent (Wave-2 re-audit finding 3).
TEST_DOCUMENT = TEST_FIXTURE

GENERATORS = [(contract_gen, WIRE), (deploy_gen, OPERATIONS)]


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _normalize(text: str) -> str:
    """Casefolded, alphanumerics only. Tolerates a caller reformatting a leaf for display without
    tolerating its absence."""
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def _expected_block_lines(doc, block) -> tuple[str, ...]:
    """What this block must display, taken from the REGISTRY wherever the registry owns it.

    Trusting `block.lines` meant trusting the renderer's own account of what it drew (re-audit
    `4f23f23..122cc67` finding 3). For every projection the registry can compute, the expectation
    comes from `docs.contracts.projection` instead; only structural prose and caller-composed
    sentences fall back to the recorded lines, and those are covered by the leaf check.
    """
    if block.claim_id and block.projection and block.projection not in (
            projection.COMPOSED, projection.TABLE):
        derived = projection.expected_lines(doc.registry[block.claim_id], block.projection)
        extra = [line for line in block.lines if line not in derived]
        return (*extra, *derived) if block.lines and extra else derived
    return block.lines


def _verify_page_matches_model(doc, page: str) -> None:
    """Every block line, on the page, at or after the block before it.

    This is the single comparison both the standing test and the mutation harness run, so a
    mutation cannot fail against a stricter bespoke check than the release itself applies.
    """
    squashed = "".join(page.split())
    cursor = 0
    for block in doc.blocks:
        if block.kind == "table":
            continue  # tables are read cell-wise; extract_text interleaves their columns
        for line in _expected_block_lines(doc, block):
            needle = _flat(line)
            # Registry-derived lines are located in ORDER from the running cursor, so even a short
            # one is unambiguous — it must appear after the line before it, not merely somewhere.
            # Codex removed the visible "1. v2" from the canonical block and every check passed,
            # because "v2" occurs elsewhere on the page and a length threshold skipped it.
            floor = 3 if block.claim_id and block.projection not in (
                projection.COMPOSED, projection.TABLE, "") else 12
            if len(needle) < floor:
                continue
            found = page.find(needle, cursor)
            if found < 0 and "".join(needle.split()) in squashed:
                # Content that wraps MID-TOKEN by design — the 163-byte signed body, a hex digest
                # — is reported by the extractor with a break inside it, so no space-preserving
                # search can find it. Presence is still provable; position is not, so this does
                # not advance the cursor and the block is excluded from the order guarantee.
                continue
            assert found >= 0, (
                f"{block.claim_id or block.kind}: not on the page at or after the preceding "
                f"block: {needle[:70]!r}"
            )
            cursor = found


FOOTER_BAND_INCHES = 0.62  # the stamped footer sits at 0.45in; body text never reaches this low


def body_text(path: str) -> str:
    """The page text with the page FURNITURE removed.

    A paragraph that crosses a page boundary has the footer physically between its halves, so the
    extractor reports "...the older decision KYC Tool - source abc123 Page 4 of 7 the later
    timestamp...". Any contiguous-span comparison then fails on ordinary, correct prose. Excluding
    the footer band by GEOMETRY rather than by matching its text also means a body line that
    happens to look like a footer cannot be dropped by accident.
    """
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            cutoff = page.height - FOOTER_BAND_INCHES * 72
            words = [w for w in page.extract_words() if w["top"] < cutoff]
            lines: dict[float, list] = {}
            for word in words:
                lines.setdefault(round(word["top"], 1), []).append(word)
            for top in sorted(lines):
                row = sorted(lines[top], key=lambda w: w["x0"])
                out.append(" ".join(w["text"] for w in row))
    return _flat("\n".join(out))


def _page_text(generator, tmp_path) -> str:
    path = str(tmp_path / "model.pdf")
    _build(generator).build(path)
    return body_text(path)


def _page_tables(generator, tmp_path):
    path = str(tmp_path / "model-tables.pdf")
    _build(generator).build(path)
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append([[_flat(cell or "") for cell in row] for row in table])
    return tables


@pytest.fixture(scope="module", params=GENERATORS, ids=lambda g: g[0].__name__.split(".")[-1])
def rendered(request, tmp_path_factory):
    generator, registry = request.param
    tmp = tmp_path_factory.mktemp("model")
    return generator, registry, _build(generator), _page_text(generator, tmp), _page_tables(
        generator, tmp)


# ── the model reaches the page, in order, once each, in its own section ───────────────────────────
def test_every_block_line_is_on_the_page_in_document_order(rendered):
    """Order is the property a global search throws away. Reversing the eight canonical signing
    lines leaves every one of them present and changes nothing a set comparison can see."""
    _generator, _registry, doc, page, _tables = rendered
    _verify_page_matches_model(doc, page)


def test_substantive_lines_appear_exactly_once(rendered):
    """Multiplicity. A duplicate block displays a value twice, and a reader cannot tell which one
    is authoritative — nor can a coverage count notice."""
    _generator, _registry, doc, page, _tables = rendered
    for block in doc.blocks:
        if block.kind == "table":
            continue
        for line in block.lines:
            needle = _flat(line)
            if len(needle) < UNIQUE_LINE_CHARS:
                continue
            if page.count(needle) == 0:
                squashed = "".join(page.split())
                assert squashed.count("".join(needle.split())) == 1, (
                    f"{block.claim_id or block.kind}: wrapped content is not on the page exactly "
                    f"once: {needle[:60]!r}")
                continue
            assert page.count(needle) == 1, (
                f"{block.claim_id or block.kind}: appears {page.count(needle)} times: "
                f"{needle[:70]!r}"
            )


def test_every_claim_renders_inside_its_declared_section(rendered):
    """A claim in the wrong section is a claim under the wrong heading. The reader takes the
    heading as its scope, so `decided_at` guidance filed under Retention is misleading even though
    every word of it is correct."""
    _generator, _registry, doc, page, _tables = rendered
    headings = [
        (section.section_id, page.find(_flat(section.title)))
        for section in doc.sections if section.title
    ]
    assert all(offset >= 0 for _id, offset in headings), headings

    for section in doc.sections:
        if not section.title:
            continue
        start = page.find(_flat(section.title))
        later = [o for _i, o in headings if o > start]
        end = min(later) if later else len(page)
        for block in section.blocks:
            if block.claim_id is None or block.kind == "table":
                continue
            anchor = max((_flat(line) for line in block.lines), key=len, default="")
            if len(anchor) < 12:
                continue
            at = page.find(anchor)
            assert start <= at < end, (
                f"{block.claim_id} renders outside section {section.section_id!r}"
            )


def _cellnorm(cell, *, token_column: bool = False) -> str:
    """One cell's comparable text, normalized by the column's DECLARED display kind.

    Wave-2 audit finding 4: this used to erase commas in every column, on a rationale that is
    only true of token columns — `_token_cell` replaces a token list's commas with line breaks,
    so the extractor genuinely cannot return them. Applying that forgiveness everywhere hid
    punctuation loss in ordinary prose: a `_prose_cell` that dropped commas rendered "malformed
    envelope malformed payload or an invalid reviewer actor" and passed. The forgiveness is now
    scoped to the columns the block declares as token columns; a prose cell is compared exactly,
    modulo whitespace, like every other string in the document.
    """
    flat = _flat(cell)
    return _flat(re.sub(r",\s*", " ", flat)) if token_column else flat


def _matrix_rows(rows, code_columns):
    """Every row of a table normalized column by column, per the block's display schema."""
    return [
        tuple(_cellnorm(cell, token_column=index in code_columns)
              for index, cell in enumerate(row))
        for row in rows
    ]


def _verify_tables_match_model(doc, tables) -> None:
    """The COMPLETE table lane, both directions (Wave 2 F5, and F6's table half).

    Before this, tables were verified as bags: the model's cells had to APPEAR somewhere in the
    page's cell pool (cells under 12 characters skipped — YES and NO never checked at all), and a
    claim's caller-assembled rows only had to CONTAIN the claim's leaves. Reversing every
    effectiveness row, swapping the Effective? and Why values under unchanged headers, and moving
    a condition to its neighbour all certified, and an entire injected duplicate table rode along
    unnoticed, because none of that changes a bag.

    Three exact comparisons replace the bags:

    1. MODEL == REGISTRY: every claim table's recorded matrix equals
       `projection.expected_matrix(claim)` — the renderer cannot author a cell, a column title,
       or an order. (A structural table — claim_id None — has no registry matrix; its content is
       digest-pinned in NARRATION_LABELS instead, and rule 3 still holds its page rendering to
       the recorded matrix.)
    2. NO ORPHAN TABLES, EITHER WAY: every header class on the page belongs to some model block,
       and every model header class reaches the page — an injected table with a novel header is
       refused by name.
    3. PAGE == MODEL, ORDERED: per header class, the concatenation of page tables in page order
       equals the concatenation of model matrices in document order — order, column index, and
       multiplicity included, short cells included. A table split across a page break simply
       contributes its continuation rows (the repeated header names its class), and an injected
       DUPLICATE of a real table breaks the concatenation even though every one of its cells is
       already legitimate.

    Cells are normalized by the block's DECLARED display schema (`code_columns`), so the
    comma forgiveness token cells genuinely need is confined to token columns and prose cells
    are compared exactly (Wave-2 audit finding 4). Header cells always render as prose, so a
    header is exact on both sides and stays usable as the class key.
    """
    model_by_header: dict[tuple, list] = {}
    schema_by_header: dict[tuple, tuple] = {}
    for block in doc.blocks:
        if block.kind != "table" or not block.rows:
            continue
        if block.claim_id is not None:
            assert block.projection == projection.TABLE, (
                f"{block.claim_id}: a claim table renders only under the TABLE projection, "
                f"not {block.projection!r}"
            )
            wanted = projection.expected_matrix(doc.registry[block.claim_id], block.row_fields)
            assert block.rows == wanted, (
                f"{block.claim_id}: the recorded table is not the registry's matrix.\n"
                f"  registry: {wanted[:3]}...\n  recorded: {block.rows[:3]}..."
            )
        header = tuple(_cellnorm(cell) for cell in block.rows[0])
        # the CLAIM's declaration, not the block's record of what the renderer used: the verifier
        # must not take its forgiveness rule from the layer that chose the lossy rendering
        # (Wave-2 re-audit finding 2)
        schema = tuple(sorted(
            doc.registry[block.claim_id].token_columns if block.claim_id else block.code_columns))
        if block.claim_id is not None:
            assert tuple(sorted(block.code_columns)) == schema, (
                f"{block.claim_id}: the renderer drew token columns {block.code_columns} but the "
                f"claim declares {doc.registry[block.claim_id].token_columns}")
        if header in schema_by_header:
            assert schema_by_header[header] == schema, (
                f"table {header[:3]}...: two blocks share a header class but declare different "
                f"token columns ({schema_by_header[header]} vs {schema}), so the page's cells "
                "cannot be normalized unambiguously"
            )
        schema_by_header[header] = schema
        model_by_header.setdefault(header, []).append(
            _matrix_rows(block.rows[1:], schema))

    page_by_header: dict[tuple, list] = {}
    for table in tables:  # already in page order
        if not table:
            continue
        header = tuple(_cellnorm(cell) for cell in table[0])
        # the model's declared schema for this class, or none at all when the page shows a
        # table the model never recorded — which the orphan check below refuses by name
        page_by_header.setdefault(header, []).append(
            _matrix_rows(table[1:], schema_by_header.get(header, ())))

    unclaimed = sorted(set(page_by_header) - set(model_by_header))
    assert not unclaimed, (
        f"the page renders a table no model block accounts for: {unclaimed}"
    )
    missing = sorted(set(model_by_header) - set(page_by_header))
    assert not missing, f"a recorded table never reached the page: {missing}"

    for header, model_groups in model_by_header.items():
        expected = [row for group in model_groups for row in group]
        actual = [row for group in page_by_header[header] for row in group]
        assert actual == expected, (
            f"table {header[:3]}...: the page's rows are not the model's rows in order.\n"
            f"  first difference: "
            f"""{next(((a, e) for a, e in zip(actual, expected, strict=False) if a != e),
                      (len(actual), len(expected)))!r}"""
        )


def test_every_table_is_the_registry_matrix_on_the_page_in_order(rendered):
    _generator, _registry, doc, _page, tables = rendered
    _verify_tables_match_model(doc, tables)


# ── the page is the model and NOTHING ELSE (Wave 2 F6, prose half) ────────────────────────────────
def _prose_stream(doc) -> str:
    """Every character the model says the page's PROSE lane displays, in document order,
    whitespace removed. Bullet and alert lines carry the en-dash the renderer prints before
    them; every other decoration is whitespace and vanishes in the squash."""
    parts = []
    for block in doc.blocks:
        decorated = block.kind == "alert" or block.projection == projection.BULLETS
        for line in block.lines:
            parts.append(("–" + str(line)) if decorated else str(line))
    return "".join("".join(part.split()) for part in parts)


def _page_prose(path: str) -> str:
    """Every character actually drawn in the prose lane: body words outside every table's
    bounding box and above the footer band, in reading order, whitespace removed."""
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            cutoff = page.height - FOOTER_BAND_INCHES * 72
            boxes = [t.bbox for t in page.find_tables()]

            def in_table(word, boxes=boxes) -> bool:
                cx = (word["x0"] + word["x1"]) / 2
                cy = (word["top"] + word["bottom"]) / 2
                return any(x0 <= cx <= x1 and y0 <= cy <= y1 for (x0, y0, x1, y1) in boxes)

            words = [w for w in page.extract_words() if w["top"] < cutoff and not in_table(w)]
            lines: dict[float, list] = {}
            for word in words:
                lines.setdefault(round(word["top"], 1), []).append(word)
            for top in sorted(lines):
                row = sorted(lines[top], key=lambda w: w["x0"])
                out.append("".join(w["text"] for w in row))
    return "".join(out)


def _verify_prose_stream(doc, page_prose: str) -> None:
    """PAGE == MODEL, totally. The order/once/section checks all ask whether the model reaches
    the page; none of them ask what ELSE the page carries, which is exactly the R15 F6 witness:
    `doc.story.append(Paragraph("Return 2xx before COMMIT."))` — every word already legitimate,
    no block, visible to a reader, invisible to every model-walking check (survey probe: 123
    passed). Character-for-character equality of the two streams refuses any injection, any
    deletion, any reorder, and any duplication at once, with no floor a short line can duck
    under."""
    expected = _prose_stream(doc)
    if page_prose == expected:
        return
    at = next((i for i, (a, b) in enumerate(zip(page_prose, expected, strict=False)) if a != b),
              min(len(page_prose), len(expected)))
    raise AssertionError(
        "the page's prose is not exactly the model's prose.\n"
        f"  first difference at {at} (page has {len(page_prose)} chars, "
        f"model expects {len(expected)}):\n"
        f"  page:  ...{page_prose[max(0, at - 40):at + 60]!r}...\n"
        f"  model: ...{expected[max(0, at - 40):at + 60]!r}..."
    )


# ── the furniture lane: the footer band is derived, not free (Wave-2 audit finding 3) ────────────
def _page_footers(path: str) -> list[str]:
    """Everything drawn in each page's footer band, left to right, in page order.

    Read by GEOMETRY — the same band `_page_prose` subtracts — and returned WHOLE rather than
    split by position: a positional split is a heuristic that can be wrong (a long title crosses
    the page midpoint), while the joined band is exact and leaves nowhere for extra furniture
    text to hide.
    """
    found = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            cutoff = page.height - FOOTER_BAND_INCHES * 72
            words = sorted((w for w in page.extract_words() if w["top"] >= cutoff),
                           key=lambda w: w["x0"])
            found.append(_flat(" ".join(w["text"] for w in words)))
    return found


def _verify_footers(doc, path: str, module) -> None:
    """PAGE FURNITURE == THE REGISTRY'S DERIVATION FOR THIS GENERATOR, page by page.

    Wave-2 audit finding 3: `build()` took a caller-authored title, drew it in every footer and
    into the PDF metadata, and the body comparisons subtract the footer band by geometry — so
    building the real contract with the title "KYC Tool — Return 2xx before COMMIT" published a
    false instruction on all twelve pages while every content check passed. The footer-shape test
    only asked that `source`, `Page N of`, and the section appear SOMEWHERE in the band, so extra
    text was legal.

    The title is the registry's now, and this lane compares the exact per-page tuple — title,
    that page's own section, the stamped revision, and an honest `Page N of M` — to the text
    drawn in the band. Nothing else fits.

    `module` is the GENERATOR under test, and reading the expectation from it is the second half
    of the control (re-audit-2 finding 2). Looking the identity up by `doc.document_id` proved
    only that some registered title was stamped consistently, because that id is the generator's
    own choice: with `techcraft_deployment_guide.DOCUMENT_ID = CONTRACT` the six-page operational
    guide published as `KYC Tool — Platform Integration Contract` — cover, twelve footers, PDF
    metadata — and passed. The question this lane asks now is the one that has an outside answer:
    which document is THIS module bound to publish, and is that what the page says?
    """
    published = documents.identity_of(module.__name__)
    expected = [
        _flat(f"{published.footer_line(doc.page_sections.get(number, ''), doc._revision)} "
              f"Page {number} of {doc._total_pages}")
        for number in range(1, doc._total_pages + 1)
    ]
    actual = _page_footers(path)
    assert actual == expected, (
        "the page furniture is not the manifest's derivation.\n"
        f"  first difference: "
        f"""{next(((a, e) for a, e in zip(actual, expected, strict=False) if a != e),
                  (len(actual), len(expected)))!r}"""
    )
    # and the sections it names are the document's own, in document order
    declared = [s.title for s in doc.sections if s.title]
    seen = [doc.page_sections.get(n, "") for n in range(1, doc._total_pages + 1)]
    assert set(seen) <= set(declared) | {""}, (
        f"a footer names a section the document does not declare: {sorted(set(seen) - set(declared))}")
    order = [declared.index(s) for s in seen if s]
    assert order == sorted(order), f"footer sections are out of document order: {seen}"


def test_the_page_furniture_is_exactly_the_manifests_derivation(rendered, tmp_path):
    generator, _registry, _doc, _page, _tables = rendered
    doc = _build(generator)
    path = str(tmp_path / "furniture.pdf")
    doc.build(path)
    _verify_footers(doc, path, generator)


def test_w2f3_a_false_title_cannot_be_supplied_at_build_time():
    """The original witness, refused at the signature: no title parameter, and no identity
    OBJECT either — a Doc names its document by id and looks the identity up itself."""
    doc = _build(contract_gen)
    with pytest.raises(TypeError):
        doc.build("/tmp/unused.pdf", "KYC Tool — Return 2xx before COMMIT")
    import inspect

    assert "title" not in inspect.signature(render_module.Doc.build).parameters
    with pytest.raises(KeyError, match="no document identity"):
        render_module.Doc(WIRE, "KYC Tool — Return 2xx before COMMIT")


def test_w3f3_a_generator_cannot_publish_an_identity_it_authors(tmp_path):
    """Wave-2 re-audit finding 3, the exact trigger. Monkeypatching the generator's MANIFEST used
    to republish a false title on every page with `_top_level_verify` green, because the verifier
    derived its expectation from the same object the renderer stamped.

    There is no such object now: the generator holds an ID, and both the stamp and the check read
    the closed identity registry. Setting the ID to anything unregistered fails at build, and
    monkeypatching the module's DOCUMENT_ID to another REGISTERED document publishes that
    document's real title — which the furniture lane, reading the identity for the document the
    verifier was asked about, refuses.
    """
    monkey = contract_gen.DOCUMENT_ID
    try:
        contract_gen.DOCUMENT_ID = "KYC Tool — Return 2xx before COMMIT"
        with pytest.raises(KeyError, match="no document identity"):
            _build(contract_gen)
    finally:
        contract_gen.DOCUMENT_ID = monkey


@pytest.mark.parametrize(
    ("module", "registry", "stolen"),
    [(deploy_gen, OPERATIONS, documents.CONTRACT),
     (contract_gen, WIRE, documents.DEPLOYMENT_GUIDE)],
    ids=["guide-as-contract", "contract-as-guide"])
def test_w4f2_a_generator_cannot_publish_another_registered_documents_identity(
        module, registry, stolen, tmp_path, monkeypatch):
    """Re-audit-2 finding 2, both swaps, through the gate a release actually runs.

    The identity registry being closed stopped a generator inventing a title; it did not stop one
    publishing under another registered document's. `techcraft_deployment_guide.DOCUMENT_ID =
    CONTRACT` shipped the six-page operational guide with the cover, all six footers and the PDF
    metadata reading `KYC Tool — Platform Integration Contract`, and `_top_level_verify` passed —
    a reader could receive the guide AS the contract with the verifier green. The old check here
    only compared the two documents' footers to each other, which proves the titles differ today,
    not that either document used its own.

    The registry binds generator to document now, and the furniture lane asks it about the module
    under test rather than reading the id the module chose. A stolen identity is a real, pinned
    title on the wrong body — and that is exactly what fails.
    """
    monkeypatch.setattr(module, "DOCUMENT_ID", stolen)
    with pytest.raises(AssertionError, match="not the .*derivation"):
        _top_level_verify(module, registry, tmp_path)


def test_w4f2_the_registry_binds_each_generator_to_the_document_it_publishes():
    """And the binding is the registry's, not a restatement of what the generators already say:
    a module asks which document it is, so the two sides cannot agree by construction."""
    import inspect

    bindings = {
        contract_gen: documents.CONTRACT,
        deploy_gen: documents.DEPLOYMENT_GUIDE,
    }
    for module, expected in bindings.items():
        assert documents.bound_id(module.__name__) == expected
        assert expected == module.DOCUMENT_ID, (
            f"{module.__name__} publishes as {module.DOCUMENT_ID!r}, not its bound document")
        source = inspect.getsource(module)
        assert "bound_id(__name__)" in source, (
            f"{module.__name__} must ASK the registry which document it is")
        for literal in (documents.CONTRACT, documents.DEPLOYMENT_GUIDE, documents.TEST_FIXTURE):
            assert f'"{literal}"' not in source, (
                f"{module.__name__} names a document id literally; the binding is the registry's")
    # closed both ways: every published identity is bound to one module, and an unbound module
    # has no document at all
    published = {i.id for i in documents.DOCUMENT_IDENTITIES.values() if i.module}
    assert set(documents.PUBLISHED_BY_MODULE.values()) == published == set(bindings.values())
    with pytest.raises(KeyError, match="publishes no registered document"):
        documents.bound_id("docs.generators.not_a_generator")


def test_w3f3_the_identity_registry_is_the_only_source_of_furniture_text():
    """Structural: neither generator holds identity TEXT, so there is nothing at that layer for a
    coherent edit to falsify. The registry is the one home, and its complete projection is pinned
    by the authority suite."""
    import inspect

    for module in (contract_gen, deploy_gen):
        source = inspect.getsource(module)
        published = documents.identity_of(module.__name__)
        assert published.title not in source, (
            f"{module.__name__} restates its own title; identity belongs to the registry alone")
        assert not hasattr(module, "MANIFEST")


def test_w2f3_a_tampered_footer_title_fails_the_furniture_lane(tmp_path):
    """And if the stamp itself is subverted — an identity swapped between layout and stamping —
    the lane refuses, so the control is the comparison and not merely the shape of the API."""
    doc = _build(contract_gen)
    hostile = documents.DocumentIdentity(
        id="hostile", title="KYC Tool — Return 2xx before COMMIT", out="hostile.pdf")
    real = render_module._stamped_canvas

    def stamped_with_lie(_identity, revision, page_sections=None):
        return real(hostile, revision, page_sections)

    render_module._stamped_canvas = stamped_with_lie
    try:
        path = str(tmp_path / "false-footer.pdf")
        doc.build(path)
    finally:
        render_module._stamped_canvas = real
    with pytest.raises(AssertionError, match="not the .*derivation"):
        _verify_footers(doc, path, contract_gen)


@pytest.mark.parametrize("generator", [contract_gen, deploy_gen], ids=["contract", "guide"])
def test_w2f3_the_real_release_paths_publish_governed_furniture(generator, tmp_path, monkeypatch):
    """Both real `main()` entry points, executed — the paths a release actually runs, not a
    rebuilt Doc that happens to resemble them."""
    monkeypatch.chdir(tmp_path)
    if generator is contract_gen:
        out = generator.main(["--integration-contact", SAMPLE_CONTACT,
                             "--response-due-date", SAMPLE_DUE_DATE])
        doc = generator.build(contact=SAMPLE_CONTACT, due_date=SAMPLE_DUE_DATE)
    else:
        out = generator.main()
        doc = generator.build()
    doc.build(str(tmp_path / "twin.pdf"))
    assert _page_footers(out) == _page_footers(str(tmp_path / "twin.pdf"))
    _verify_footers(doc, out, generator)
    with pdfplumber.open(out) as pdf:
        assert (pdf.metadata.get("Title") or "") == documents.identity(generator.DOCUMENT_ID).title


def test_the_page_prose_is_exactly_the_model_and_nothing_else(rendered, tmp_path):
    generator, _registry, doc, _page, _tables = rendered
    path = str(tmp_path / "prose-total.pdf")
    _build(generator).build(path)
    _verify_prose_stream(doc, _page_prose(path))


# ── attribution: nothing authoritative is said outside the claim that owns it ─────────────────────
def test_exclusive_terms_appear_only_in_their_own_claim(rendered):
    """The appended-contradictory-paragraph mutation. A claim being correct does not make the page
    correct: an unclaimed paragraph next to it is equally visible and was attributable to nothing."""
    _generator, registry, doc, _page, _tables = rendered
    for claim in registry.claims:
        for term in claim.exclusive_terms:
            for block in doc.blocks:
                if block.claim_id is not None:
                    continue  # another claim may reference the subject; its authority test covers it
                if block.kind == "heading":
                    continue  # a heading NAMES the subject; it does not assert anything about it
                haystack = " ".join(list(block.lines) + [c for r in block.rows for c in r])
                # word boundaries: "COMMIT" must not match "commitment"
                hit = re.search(rf"\b{re.escape(term)}\b", haystack, re.IGNORECASE)
                assert hit is None, (
                    f"{term!r} is owned by {claim.id} but appears in unattributed "
                    f"{block.kind} prose, which nothing verifies"
                )


def test_at_least_the_two_mutated_claims_declare_exclusive_terms():
    """Guard the guard: an empty `exclusive_terms` everywhere makes the test above vacuous."""
    declared = [c.id for c in (*WIRE.claims, *OPERATIONS.claims) if c.exclusive_terms]
    assert "WIRE.CALLBACK.RECEIVER_TXN" in declared
    assert "WIRE.ORDERING.NO_DECIDED_AT" in declared


def test_every_claim_block_carries_visible_lines(rendered):
    """`_emit` refuses a flowable with no visible text, so a block that displays nothing cannot be
    recorded — which is the 'delete a visible block while preserving its id' mutation."""
    _generator, _registry, doc, _page, _tables = rendered
    for block in doc.blocks:
        if block.claim_id is None:
            continue
        assert block.lines or block.rows, f"{block.claim_id} recorded nothing visible"


def test_claim_ids_and_blocks_agree(rendered):
    """`rendered` (the id list) and the block model must describe the same document."""
    _generator, _registry, doc, _page, _tables = rendered
    from_blocks = Counter(b.claim_id for b in doc.blocks if b.claim_id and b.role == "value")
    assert from_blocks == Counter(doc.rendered)
    # a note is attributed but is not the claim's value, so it never inflates coverage
    # every block is a VALUE block now: the old "attributed but unverified" note role is gone
    assert not [b for b in doc.blocks if b.role != "value"], (
        "a block still claims the retired note role")


# ── the named mutations from the audit, each of which used to pass ────────────────────────────────
@contextlib.contextmanager
def _mutated(registry, claim_id, **changes):
    """Swap one claim in place for the duration of a test.

    In place, not a copy: the generators, the authority map and this module all hold the same
    Registry object, so a replacement bound to one name would leave the others verifying the
    original — and the mutation would "fail" for the wrong reason, or pass.
    """
    original = registry.claims
    mutant = dataclasses.replace(registry[claim_id], **changes)
    object.__setattr__(
        registry, "claims", tuple(mutant if c.id == claim_id else c for c in original))
    try:
        yield
    finally:
        object.__setattr__(registry, "claims", original)


def _top_level_verify(generator, registry, tmp_path):
    """Everything a release runs: the authority map, the rendered-page comparison, and the
    ordered table-matrix comparison (Wave 2 F5 — a mutation that only reorders or re-columns a
    table is invisible to the prose lane, so leaving tables out of this harness would let every
    table mutation 'fail' against a bespoke check the release never runs)."""
    from .test_contract_registry_authority import (
        AUTHORITY_VERIFIERS,
        _receipt_problems,
    )
    from .test_contract_registry_authority import (
        OPERATIONS as OPS_REGISTRY,
    )
    from .test_contract_registry_authority import (
        WIRE as WIRE_REGISTRY,
    )

    for claim in registry.claims:
        AUTHORITY_VERIFIERS[claim.id]()
    # …and the receipt closure, so the release verifier covers the REVIEWED lane too (Wave-2
    # re-audit finding 1). Codex's inverted sentences passed `_top_level_verify` precisely
    # because this ran only in the authority suite: an executed answer cannot judge English, so
    # the pin that does has to be part of the same gate.
    problems = _receipt_problems((WIRE_REGISTRY, OPS_REGISTRY))
    assert not problems, "\n".join(problems)

    doc = _build(generator)
    path = str(tmp_path / "mutant.pdf")
    doc.build(path)
    _verify_page_matches_model(doc, body_text(path))
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append([[_flat(cell or "") for cell in row] for row in table])
    _verify_tables_match_model(doc, tables)
    _verify_prose_stream(doc, _page_prose(path))
    # the band the other three lanes subtract (Wave-2 finding 3), checked against the identity
    # the REGISTRY binds to this generator rather than the one the generator picked
    _verify_footers(doc, path, generator)


def _hmac_rule_saying(answer: str):
    """The HMAC-set statement re-declared with the other answer, so it publishes the other
    sentence. Used by the mutation table below."""
    from docs.contracts.statements import Statement

    published = OPERATIONS.value("OPS.CONFIG.HMAC_SET_RULE")
    return Statement(fact=published.fact, answer=answer,
                     alternatives=dict(published.alternatives))


MUTATIONS = [
    # Every one of these left the previous top-level suite green.
    ("renaming a canonical signing line", WIRE, contract_gen, "WIRE.SIGN.CANONICAL",
     {"value": ("v2", "NOT_KEY_ID", "direction", "method", "path?query", "timestamp", "slot",
                "sha256(body) hex")}),
    ("swapping a required HMAC variable for an unrelated real setting", OPERATIONS, deploy_gen,
     "OPS.CONFIG.HMAC_SET",
     {"value": ("KYC_PLATFORM_HMAC_SECRET", "KYC_DATABASE_URL", "KYC_HMAC_INBOUND_SECRET",
                "KYC_HMAC_OUTBOUND_KEY_ID", "KYC_HMAC_OUTBOUND_SECRET",
                "KYC_HMAC_V1_INBOUND_SUNSET_AT", "KYC_HMAC_V1_OUTBOUND_SUNSET_AT",
                "KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS")}),
    # the floor/date promise is its own claim now (Wave-2 finding 1). Declaring the other answer
    # publishes "Production accepts a subset…", which the executed boundary refuses.
    ("advertising a partial HMAC set as acceptable", OPERATIONS, deploy_gen,
     "OPS.CONFIG.HMAC_SET_RULE",
     {"value": _hmac_rule_saying("accepts_partial")}),
    ("prescribing a rolling security cutover", OPERATIONS, deploy_gen,
     "OPS.RELEASE.CLASSIFICATION",
     {"value": "Releases without a migration are always safe to deploy rolling, including "
               "security releases."}),
    ("deleting the accepted 024 requirements", WIRE, contract_gen, "WIRE.ORDERING.BOOTSTRAP_024",
     {"value": "NOT BUILT"}),
    ("reversing the eight visible canonical lines", WIRE, contract_gen, "WIRE.SIGN.CANONICAL",
     {"value": ("sha256(body) hex", "slot", "timestamp", "path?query", "method", "direction",
                "key_id", "v2")}),
    ("removing the visible meaning while keeping the id", WIRE, contract_gen,
     "WIRE.ORDERING.NO_DECIDED_AT", {"value": "decided_at is present on every callback."}),
    ("making the receiver answer before it commits", WIRE, contract_gen,
     "WIRE.CALLBACK.RECEIVER_TXN",
     {"value": ("Verify the signature.", "Only then return 2xx.",
                "COMMIT in your own time afterwards.")}),
]


@pytest.mark.parametrize(
    ("description", "registry", "generator", "claim_id", "changes"),
    MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_each_demonstrated_mutation_fails_the_top_level_verifier(
        description, registry, generator, claim_id, changes, tmp_path):
    """The list Codex proved could pass. Each one is applied to the live registry and driven
    through the SAME entry point a release runs — the authority map plus the rendered-page
    comparison — so a mutation that only a bespoke check would catch does not count."""
    with _mutated(registry, claim_id, **changes), pytest.raises(AssertionError):
        _top_level_verify(generator, registry, tmp_path)


def test_the_unmutated_documents_pass_the_same_verifier(tmp_path):
    """Guard the guard: if `_top_level_verify` failed unconditionally, every row above would
    'pass' while proving nothing."""
    for generator, registry in GENERATORS:
        _top_level_verify(generator, registry, tmp_path)


def test_mutation_a_contradictory_unclaimed_paragraph_fails(tmp_path):
    """An unattributed paragraph that contradicts a claim, added straight to the document."""
    doc = _build(contract_gen)
    doc.p("You may return 2xx before committing; we will retry if you fail later.")

    offenders = []
    for claim in WIRE.claims:
        for term in claim.exclusive_terms:
            for block in doc.blocks:
                if block.claim_id == claim.id:
                    continue
                if term.lower() in " ".join(block.lines).lower():
                    offenders.append((claim.id, term, block.claim_id))
    assert offenders, "the contradictory paragraph was not attributed to anything and passed"


def test_mutation_moving_a_claim_to_the_wrong_section_fails(tmp_path):
    """Rendered under Retention, the `decided_at` prohibition reads as a retention rule."""
    doc = _build(contract_gen)
    misfiled = [s for s in doc.sections if s.section_id == "retention"][0]
    ordering = [s for s in doc.sections if s.section_id == "ordering"][0]
    block = next(b for b in ordering.blocks if b.claim_id == "WIRE.ORDERING.NO_DECIDED_AT")
    ordering.blocks.remove(block)
    misfiled.blocks.append(block)
    assert doc.section_of("WIRE.ORDERING.NO_DECIDED_AT") == "retention"
    # and the real check would reject it, because the text renders before the Retention heading
    path = str(tmp_path / "misfiled.pdf")
    doc.build(path)
    with pdfplumber.open(path) as pdf:
        page = _flat("\n".join(p.extract_text() or "" for p in pdf.pages))
    start = page.find(_flat(misfiled.title))
    anchor = max((_flat(line) for line in block.lines), key=len)
    assert page.find(anchor) < start, "the misfiled claim would have to move on the page too"


# ── the F5/F6 table witnesses, each proven certifying before the matrix lane existed ──────────────
#
# Survey probes against the pre-fold tree (`a4d9be4`): with the caller still assembling rows,
# reversing every effectiveness row, swapping the Effective?/Why values under unchanged headers,
# moving a Why to its neighbouring row, and appending a whole reversed duplicate of the table
# directly to the story each left BOTH document suites fully green (123 passed, every time).
# The tests below hold the exact witnesses against the comparison a release now runs.

def _extracted_tables(doc, tmp_path, name):
    path = str(tmp_path / f"{name}.pdf")
    doc.build(path)
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append([[_flat(cell or "") for cell in row] for row in table])
    return tables


TABLE_BETRAYALS = [
    ("drawing every row reversed", lambda rows: tuple(reversed(rows))),
    ("swapping the Effective? and Why values under unchanged headers",
     lambda rows: tuple((*row[:3], row[4], row[3]) for row in rows)),
    ("moving one row's Why onto its neighbour",
     lambda rows: ((*rows[0][:4], rows[1][4]), (*rows[1][:4], rows[0][4]), *rows[2:])),
    ("duplicating the first row over its neighbour",
     lambda rows: (rows[0], rows[0], *rows[2:])),
]


@pytest.mark.parametrize(("label", "mutate"), TABLE_BETRAYALS, ids=[t[0] for t in TABLE_BETRAYALS])
def test_a_renderer_that_draws_other_than_its_recorded_matrix_fails(tmp_path, label, mutate):
    """The hostile half the model cannot see: the renderer RECORDS the honest registry matrix
    and DRAWS something else. Only the page-side ordered comparison can refuse this — a bag of
    cells cannot, which is how all four of these certified before the fold."""
    claim_id = "WIRE.CALLBACK.EFFECTIVENESS"
    real = render_module.Doc.claim_table

    def hostile(self, cid, widths, heading=None, row_fields=()):
        if cid != claim_id:
            return real(self, cid, widths, heading=heading, row_fields=row_fields)
        claim = self.registry[cid]
        matrix = projection.expected_matrix(claim, row_fields)
        drawn = (matrix[0], *mutate(matrix[1:]))
        data = [[render_module.Paragraph(render_module.escape(h), render_module.CELLB)
                 for h in drawn[0]]]
        for row in drawn[1:]:
            data.append([render_module._prose_cell(cell, widths[i])
                         for i, cell in enumerate(row)])
        table = render_module.Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(render_module._TABLE_STYLE)
        self._story.append(table)
        self.rendered.append(cid)
        self._add(render_module.Block(kind="table", claim_id=cid, lines=(), rows=matrix,
                                      projection=projection.TABLE, row_fields=row_fields))

    render_module.Doc.claim_table = hostile
    try:
        doc = _build(contract_gen)
    finally:
        render_module.Doc.claim_table = real
    tables = _extracted_tables(doc, tmp_path, "betrayal")
    with pytest.raises(AssertionError, match="not the model's rows in order"):
        _verify_tables_match_model(doc, tables)


@pytest.mark.parametrize(("label", "mutate"), TABLE_BETRAYALS, ids=[t[0] for t in TABLE_BETRAYALS])
def test_a_recorded_matrix_that_is_not_the_registrys_fails(label, mutate):
    """The coherent half: the renderer records exactly what it draws, but neither is the
    registry's matrix. MODEL == REGISTRY refuses it before the page is even consulted, so a
    coherent dual mutation cannot certify the way the caller-assembled rows once did."""
    doc = _build(contract_gen)
    for section in doc.sections:
        for i, block in enumerate(section.blocks):
            if block.claim_id == "WIRE.CALLBACK.EFFECTIVENESS" and block.kind == "table":
                tampered = (block.rows[0], *mutate(block.rows[1:]))
                section.blocks[i] = dataclasses.replace(block, rows=tampered)
    with pytest.raises(AssertionError, match="not the registry's matrix"):
        _verify_tables_match_model(doc, [])


def _injected_table(matrix, widths):
    data = [[render_module.Paragraph(render_module.escape(h), render_module.CELLB)
             for h in matrix[0]]]
    for row in matrix[1:]:
        data.append([render_module._prose_cell(cell, widths[i]) for i, cell in enumerate(row)])
    table = render_module.Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(render_module._TABLE_STYLE)
    return table


def test_an_injected_duplicate_table_fails_the_concatenation(tmp_path):
    """The R15 F6 table witness, verbatim: a whole reversed duplicate of the effectiveness table
    appended straight to the story — no block, no claim, every word already legitimate. The
    survey probe proved it certified (123 passed). Multiplicity in the per-header concatenation
    refuses it now: the header class gains rows the model never recorded."""
    doc = _build(contract_gen)
    claim = doc.registry["WIRE.CALLBACK.EFFECTIVENESS"]
    matrix = projection.expected_matrix(claim)
    widths = [0.6 * render_module.INCH, 1.85 * render_module.INCH, 1.2 * render_module.INCH,
              1.15 * render_module.INCH, 2.15 * render_module.INCH]
    doc._story.append(_injected_table((matrix[0], *reversed(matrix[1:])), widths))
    tables = _extracted_tables(doc, tmp_path, "dup-table")
    with pytest.raises(AssertionError, match="not the model's rows in order"):
        _verify_tables_match_model(doc, tables)


def test_an_injected_novel_header_table_is_refused_by_name(tmp_path):
    """A table whose header tuple belongs to no model block — built from words the documents
    already use, so the vocabulary check cannot be the control that speaks."""
    doc = _build(contract_gen)
    doc._story.append(_injected_table(
        (("Record", "Why"), ("record it", "the platform recovers it")),
        [2 * render_module.INCH, 3 * render_module.INCH]))
    tables = _extracted_tables(doc, tmp_path, "novel-table")
    with pytest.raises(AssertionError, match="no model block accounts for"):
        _verify_tables_match_model(doc, tables)


def test_claim_table_accepts_no_caller_rows_or_headers():
    """The escape hatch is gone at the signature, not policed by convention: there is no
    parameter through which a caller could hand claim_table a cell or a column title."""
    doc = _build(contract_gen)
    with pytest.raises(TypeError):
        doc.claim_table("WIRE.CALLBACK.LEGEND", [1 * render_module.INCH], rows=[("a", "b")])
    with pytest.raises(TypeError):
        doc.claim_table("WIRE.CALLBACK.LEGEND", [1 * render_module.INCH],
                        headers=("Token", "Meaning"))
    import inspect

    params = inspect.signature(render_module.Doc.claim_table).parameters
    assert "rows" not in params and "headers" not in params


def test_a_table_claim_without_declared_headers_cannot_render():
    """expected_matrix refuses a claim with no declared columns, so the registry cannot quietly
    hand column-title authorship back to a caller."""
    doc = Doc(Registry(name="headless", claims=(
        Claim(id="X.NOHEAD", value=(("a", "b"),), authority="test"),
    )), TEST_DOCUMENT)
    with pytest.raises(ValueError, match="declares no columns"):
        doc.claim_table("X.NOHEAD", [1 * render_module.INCH, 1 * render_module.INCH])
    with pytest.raises(ValueError, match="does not fit the declared"):
        projection.expected_matrix(Claim(id="X.WIDTH", value=(("a", "b", "c"),), authority="test",
                                         columns=(Column("One", PROSE), Column("Two", PROSE))))


def test_w2f4_a_prose_cell_that_loses_its_commas_fails(tmp_path):
    """Wave-2 audit finding 4, the exact trigger: a `_prose_cell` that eats commas before
    drawing. The live status row rendered "malformed envelope malformed payload or an invalid
    reviewer actor" and passed every comparison, because the forgiveness token cells need was
    applied to every column. Scoped to declared token columns, this refuses."""
    real = render_module._prose_cell

    def comma_eating(text, width):
        return real(str(text).replace(",", ""), width)

    render_module._prose_cell = comma_eating
    try:
        doc = _build(contract_gen)
    finally:
        render_module._prose_cell = real
    tables = _extracted_tables(doc, tmp_path, "comma-eaten")
    with pytest.raises(AssertionError, match="not the model's rows in order"):
        _verify_tables_match_model(doc, tables)


def test_w2f4_token_columns_still_forgive_their_own_line_breaks(rendered):
    """Guard the guard: the forgiveness must survive where it is real. The event table's payload
    columns are token cells whose commas the renderer genuinely replaces with line breaks, so
    scoping the rule must not make honest documents fail."""
    _generator, _registry, doc, _page, tables = rendered
    commas = [
        cell
        for b in doc.blocks if b.kind == "table" and b.code_columns
        for row in b.rows[1:]
        for i, cell in enumerate(row) if i in b.code_columns and "," in cell
    ]
    if not commas:
        pytest.skip("this document's token columns carry no comma-separated lists")
    _verify_tables_match_model(doc, tables)


@contextlib.contextmanager
def _swapped_claim(registry, claim_id, **changes):
    """Swap one claim in place for the duration of a test — in place, so every holder of the
    Registry object (generators, the authority map, this module) sees the mutant."""
    original = registry.claims
    mutant = dataclasses.replace(registry[claim_id], **changes)
    object.__setattr__(
        registry, "claims", tuple(mutant if c.id == claim_id else c for c in original))
    try:
        yield
    finally:
        object.__setattr__(registry, "claims", original)


LIE_ACK = ("A 2xx before your commit is recoverable and safe; the word unrecoverable is only a "
           "label and at-least-once will retry it after a lost commit.")
LIE_PLAYBOOK = ("Do not EXECUTE from the named playbook. It is merely a reference; plan and run "
                "the cutover from this summary instead.")


@pytest.mark.parametrize(("claim_id", "answer", "lie"), [
    ("WIRE.CALLBACK.ACK_CONSEQUENCE", "unrecoverable", LIE_ACK),
    ("OPS.CUTOVER.EXECUTION_SOURCE", "playbook_only", LIE_PLAYBOOK),
], ids=["early-2xx", "playbook-optional"])
def test_w3f1_a_constructible_inversion_fails_the_release_verifier(claim_id, answer, lie,
                                                                   tmp_path):
    """Wave-2 re-audit finding 1, both witnesses verbatim.

    Codex's point was exact and I had overclaimed: the token rule is a SUBSTRING check, and
    substring membership is not meaning. Each of these sentences carries its answer's own token,
    avoids its sibling's, constructs cleanly, and inverts what the document tells TechCraft — one
    says an early 2xx is recoverable while the publisher terminalizes the row, the other tells
    operators not to execute from the safety playbook.

    Nothing here pretends a machine now understands them. The ANSWER is executed; the SENTENCES
    are reviewed English, pinned per branch, and the release verifier runs that closure — so a
    rewritten sentence is refused as an unreviewed edit, at the same gate a release passes
    through, which is where these two used to slip by.
    """
    from docs.contracts.statements import Statement

    registry = WIRE if claim_id.startswith("WIRE.") else OPERATIONS
    generator = contract_gen if registry is WIRE else deploy_gen
    published = registry.value(claim_id)
    alternatives = dict(published.alternatives)
    alternatives[answer] = lie
    hostile = Statement(fact=published.fact, answer=answer, alternatives=alternatives)
    assert hostile.text == lie, "the witness must actually be constructible"

    with (
        _swapped_claim(registry, claim_id, value=hostile),
        pytest.raises(AssertionError, match="changed since it was reviewed"),
    ):
        _top_level_verify(generator, registry, tmp_path)


def test_w3f1_the_token_rule_is_declared_defense_in_depth_not_authority():
    """Guard against re-overclaiming: the module must say plainly that its substring rule does
    not judge meaning, and the sentences must be covered by the reviewed lane rather than the
    verifier-bound one."""
    from docs.contracts import statements as statements_module

    from .test_contract_registry_authority import REGISTRY_PROSE_PINS, VERIFIER_BOUND

    doc = statements_module.__doc__ or ""
    assert "DEFENSE IN DEPTH" in doc.upper() and "not meaning" in doc
    branches = [k for k in REGISTRY_PROSE_PINS if "Statement.alternatives{" in k[1]]
    assert len(branches) >= 20, "every alternative branch must sit in the reviewed lane"
    assert not [k for k in VERIFIER_BOUND if "Statement.alternatives" in k[1]], (
        "an alternative sentence is reviewed prose, never verifier-bound English")


def test_w3f2_a_caller_cannot_declare_a_prose_column_lossy(tmp_path):
    """Wave-2 re-audit finding 2, the exact trigger. `code_columns` used to be a caller argument
    that `_emit` recorded and the page comparison then TRUSTED, so a caller could mark a prose
    column a token column and have its comma loss forgiven by a check reading its own
    declaration: the live 200-row rendered without punctuation and `_top_level_verify` passed.

    The declaration is the claim's now. The parameter is gone from the signature, and if a
    renderer draws token columns the claim does not declare, the comparison says so by name.
    """
    doc = _build(contract_gen)
    import inspect

    assert "code_columns" not in inspect.signature(render_module.Doc.claim_table).parameters
    with pytest.raises(TypeError):
        doc.claim_table("WIRE.INGEST.STATUS", [1 * render_module.INCH], code_columns=(1,))

    # and the recorded schema must equal the claim's, so a renderer that draws something else is
    # refused rather than believed
    for section in doc.sections:
        for i, block in enumerate(section.blocks):
            if block.claim_id == "WIRE.INGEST.STATUS" and block.kind == "table":
                section.blocks[i] = dataclasses.replace(block, code_columns=(1,))
    tables = _extracted_tables(doc, tmp_path, "hostile-schema")
    with pytest.raises(AssertionError, match="the claim declares"):
        _verify_tables_match_model(doc, tables)


def test_w4f1_a_column_role_cannot_exist_apart_from_the_column_it_governs():
    """Re-audit-2 finding 1, the structural half. The declaration used to be a parallel tuple of
    integers — a second list to keep in step with the headers, and a field a coherent
    `dataclasses.replace` could rewrite. There is no such field: a role is a member of a closed
    set, written on the column it governs, so 'outside its table' and 'aimed at the wrong column'
    are not states this registry can be in.
    """
    for registry in (WIRE, OPERATIONS):
        for claim in registry.claims:
            assert len(claim.headers) == len(claim.columns)
            for index in claim.token_columns:
                assert 0 <= index < len(claim.columns), (claim.id, index)
    with pytest.raises(TypeError):  # no field of this name is left to replace
        dataclasses.replace(WIRE["WIRE.INGEST.STATUS"], token_columns=(1,))
    with pytest.raises(ValueError, match="not a ColumnRole"):
        Column("One", "token")
    with pytest.raises(ValueError, match="typed `Column` records"):
        Claim(id="X.BAD", value=(("a", "b"),), authority="t", columns=("One", "Two"))


def test_w4f1_a_registry_edit_cannot_make_a_prose_column_lossy(tmp_path):
    """Re-audit-2 finding 1, Codex's exact witness, through the gate a release actually runs.

    Marking `WIRE.INGEST.STATUS`'s Meaning column as a token column is a coherent registry edit:
    every row still fits, the renderer honours it, and the page comparison then forgives the
    commas `_token_cell` destroys. On the pre-fold tree it printed `(stored response verbatim) or
    an inline reviewer.manual_approve which runs without a queued job` — punctuation stripped
    from externally-binding text — with `_receipt_problems` and `_top_level_verify` both green,
    because the display schema was classified as publishing no text.

    The role is reviewed registry authority now, so the same edit changes a pinned receipt and
    the release verifier refuses it by name.
    """
    from .test_contract_registry_authority import _receipt_problems, _swapped_claim

    lossy = tuple(dataclasses.replace(column, role=ColumnRole.TOKEN) if index == 1 else column
                  for index, column in enumerate(WIRE["WIRE.INGEST.STATUS"].columns))
    with _swapped_claim(WIRE, "WIRE.INGEST.STATUS", columns=lossy):
        problems = _receipt_problems((WIRE, OPERATIONS))
        assert any("Column.role" in p and "changed since it was reviewed" in p
                   for p in problems), problems
        with pytest.raises(AssertionError, match="changed since it was reviewed"):
            _top_level_verify(contract_gen, WIRE, tmp_path)


def test_the_story_has_no_public_append():
    """The R15 F6 witness was one line: `doc.story.append(...)`. That line no longer exists —
    the public story is a read-only tuple view, so a flowable can only enter through a method
    that records its Block in the same call."""
    doc = _build(contract_gen)
    with pytest.raises(AttributeError):
        doc.story.append("anything")
    with pytest.raises(AttributeError):
        doc.story = []


def test_an_injected_prose_flowable_fails_the_total_stream(tmp_path):
    """The R15 F6 witness verbatim, forced through the PRIVATE story the way a hostile module
    would: every word already legitimate, no block, visible to a reader. The survey probe proved
    the full suites stayed green on the pre-fold tree; the total page==model prose stream is the
    control that refuses it now."""
    doc = _build(contract_gen)
    doc._story.append(render_module.Paragraph("Return 2xx before COMMIT.", render_module.BODY))
    path = str(tmp_path / "injected-prose.pdf")
    doc.build(path)
    with pytest.raises(AssertionError, match="not exactly the model's prose"):
        _verify_prose_stream(doc, _page_prose(path))


def test_a_duplicated_governed_paragraph_fails_the_total_stream(tmp_path):
    """Duplicate-and-relocate, tuned to duck every older control: the re-drawn line is a real
    recorded step SHORT enough to fall under the exactly-once check's UNIQUE_LINE_CHARS floor,
    so the multiplicity test skips it, the order check skips it, and the vocabulary is all
    legitimate. Character equality of the total stream has no floor, so it is the one control
    that refuses the page carrying the model twice."""
    doc = _build(contract_gen)
    block = next(b for b in doc.blocks if b.claim_id == "WIRE.CALLBACK.RECEIVER_TXN")
    line = next(str(li) for li in block.lines if len(_flat(li)) < UNIQUE_LINE_CHARS)
    doc._story.append(render_module.Paragraph(render_module.escape(line), render_module.BODY))
    path = str(tmp_path / "dup-prose.pdf")
    doc.build(path)
    with pytest.raises(AssertionError, match="not exactly the model's prose"):
        _verify_prose_stream(doc, _page_prose(path))


def test_the_release_inputs_still_reach_the_page(rendered):
    _generator, _registry, _doc, page, _tables = rendered
    if _generator is contract_gen:
        assert SAMPLE_CONTACT in page and SAMPLE_DUE_DATE in page


def test_no_section_id_is_reused():
    for generator, _registry in GENERATORS:
        doc = _build(generator)
        ids = [s.section_id for s in doc.sections]
        assert len(ids) == len(set(ids)), ids
        assert re.match(r"^[a-z0-9()_ -]+$", "".join(ids))


# ── the oracle is the REGISTRY, not the renderer ─────────────────────────────────────────────────
def test_the_page_matches_content_recomputed_from_the_registry(rendered):
    """Re-audit `4f23f23..122cc67` finding 3. The previous comparison used `block.lines` — the
    lines the RENDERER had just computed — so editing `claim_paragraph` to display something other
    than its claim changed the page and the expectation together and the suite stayed green.

    Here the expected content is recomputed FROM THE CLAIM through `docs.contracts.projection`,
    which the renderer does not author. A renderer that displays anything else is compared against
    what the claim actually says.
    """
    _generator, registry, doc, page, tables = rendered
    squashed = "".join(page.split())

    for block in doc.blocks:
        if block.claim_id is None:
            continue
        claim = registry[block.claim_id]

        if block.projection == projection.COMPOSED:
            # A table can no longer be COMPOSED (Wave 2 F5): claim_table derives the whole
            # matrix, so the caller has no rows to assemble. Prose composition remains — the
            # caller writes the sentence, but every leaf the claim owns must still be printed.
            # Compared NORMALIZED — casefolded with punctuation removed — because a caller
            # legitimately reformats a leaf on its way to the page (`job_max_attempts` is printed
            # as the environment variable `KYC_JOB_MAX_ATTEMPTS`). Omission and contradiction are
            # still caught; only presentation is tolerated.
            assert block.kind != "table", (
                f"{block.claim_id}: a COMPOSED table block should be unconstructible now")
            haystack = _normalize(page)
            for leaf in projection.leaf_strings(claim.value, block.row_fields):
                needle = _normalize(leaf)
                if len(needle) < 4:
                    continue
                assert needle in haystack, (
                    f"{block.claim_id}: the page omits a value the claim carries: {leaf[:70]!r}")
            continue

        if block.projection == projection.TABLE:
            # the ordered-matrix lane owns tables completely: model == registry matrix and
            # page == model in order, multiplicity included (_verify_tables_match_model)
            continue

        wanted = projection.expected_lines(claim, block.projection)
        assert wanted, f"{block.claim_id}: projection {block.projection!r} produced nothing"
        for line in wanted:
            needle = _flat(line)
            if len(needle) < 4:
                continue
            assert needle in page or "".join(needle.split()) in squashed, (
                f"{block.claim_id}: recomputed line not on the page: {needle[:70]!r}")


def test_the_renderer_cannot_author_the_expectation(rendered):
    """Guard the guard: the recorded block lines must EQUAL what the registry projection says, so
    the two halves cannot quietly diverge and leave the test above checking the renderer's own
    answer."""
    _generator, registry, doc, _page, _tables = rendered
    for block in doc.blocks:
        if block.claim_id is None or block.projection in (projection.COMPOSED, projection.TABLE):
            continue
        wanted = projection.expected_lines(registry[block.claim_id], block.projection)
        recorded = tuple(line for line in block.lines if line in wanted)
        assert set(wanted) <= set(block.lines), (
            f"{block.claim_id}: recorded {recorded} but the registry projects {wanted}")


@pytest.mark.parametrize(
    ("claim_id", "registry", "generator", "lie"),
    [
        ("WIRE.ORDERING.NO_DECIDED_AT", WIRE, contract_gen,
         "decided_at is the ordering key; sort by it."),
        ("OPS.PROCESS.DEV_WORKER_BANNED", OPERATIONS, deploy_gen,
         "dev_worker is fine in production."),
    ],
    ids=["contract", "guide"],
)
def test_a_renderer_that_displays_something_other_than_the_claim_fails(
        claim_id, registry, generator, lie, tmp_path, monkeypatch):
    """The exact mutation: make the renderer print a lie in place of the claim's value. The page
    and the renderer's own record change together — and the registry-derived expectation does
    not, so it fails."""
    from docs.generators import render as render_module

    original = render_module.Doc.claim_paragraph

    def lying_claim_paragraph(self, cid, *, style=render_module.BODY, prefix=""):
        if cid != claim_id:
            return original(self, cid, style=style, prefix=prefix)
        from docs.generators.render import Block, Paragraph, escape

        self._story.append(Paragraph(prefix + escape(lie), style))
        self.rendered.append(cid)
        self._add(Block(kind="prose", claim_id=cid, lines=(lie,),
                        projection=projection.PARAGRAPH))
        return None

    monkeypatch.setattr(render_module.Doc, "claim_paragraph", lying_claim_paragraph)
    doc = _build(generator)
    path = str(tmp_path / "lie.pdf")
    doc.build(path)
    page = body_text(path)

    wanted = projection.expected_lines(registry[claim_id], projection.PARAGRAPH)[0]
    assert _flat(wanted) not in page, "the mutation did not actually remove the claim's text"
    with pytest.raises(AssertionError):
        for block in doc.blocks:
            if block.claim_id != claim_id:
                continue
            for line in projection.expected_lines(registry[block.claim_id], block.projection):
                assert _flat(line) in page, f"{block.claim_id}: recomputed line not on the page"


# ── nothing on the page is unaccounted for ────────────────────────────────────────────────────
#
# The tests above prove the MODEL reaches the page. They say nothing about the other direction:
# a paragraph that belongs to no claim is a fact nothing verifies, and the F3 attack was exactly
# that — a contradictory commit-before-2xx paragraph appended beside the claim it contradicted.
# `exclusive_terms` catches that only for subjects somebody thought to declare in advance, which
# is the wrong shape for a control: it enumerates what to look for instead of what is allowed.
#
# So unattributed blocks are a CLOSED, PINNED set. Every one is listed here with a digest over its
# exact rendered content. Adding prose fails (an unlisted key), deleting it fails (a listed key
# with nothing behind it), and editing it fails (a digest mismatch). Re-pinning is the review, the
# same act `playbook_digest` requires of a cutover section.
#
# The residual risk is stated plainly: this makes unattributed prose a visible, reviewed decision;
# it does not make the reviewer read it. What it removes is the SILENT path — prose can no longer
# arrive on an externally-binding page without a human touching this table.

NARRATION_LABELS = {
    # ── contract ──────────────────────────────────────────────────────────────────────────────
    ("contract", "(front matter)", 0): ("b70e26fd070d110c", "Audience + scope of this document"),
    ("contract", "(front matter)", 1): ("21a2f1b76bd4de4b", "one-paragraph summary of the wire"),
    ("contract", "(front matter)", 2): ("8fa664ad08807a49",
                                        "where to send answers; interpolates the release inputs, "
                                        "which test_the_release_inputs_still_reach_the_page "
                                        "checks separately"),
    ("contract", "asks", 0): ("c77063c9a829011c", "1.1 heading + why ordering is asked for"),
    ("contract", "asks", 1): ("f7662c6b9304e6b7", "1.1 what we need now is not code"),
    ("contract", "asks", 2): ("2c5f41e9c09d3884",
                              "1.1 necessary and not sufficient; screened but "
                              "cannot clear (gate finding 10)"),
    ("contract", "asks", 3): ("3f901c37e3839bdc", "1.2 dedupe commitment"),
    ("contract", "asks", 4): ("87c2e7c956b36fc0", "1.3 callback URL and key exchange"),
    ("contract", "asks", 5): ("a6143e1512e363d3", "1.4 document upload path"),
    ("contract", "events", 0): ("4009c5156c229907", "first event creates the case; no rate limit"),
    ("contract", "events", 1): ("939d3d69279a82cb", "the label 'Envelope:'"),
    ("contract", "events", 2): ("4dc71563f2253448", "the worked envelope example"),
    ("contract", "callbacks", 0): ("c1d5f1bafb61d43a", "content type + which key signs"),
    ("contract", "signing", 0): ("6651f36142732ce8", "why v2 replaced v1"),
    ("contract", "checklist", 0): ("bb248f15a20df14d", "the go-live checklist table"),
    # ── guide ─────────────────────────────────────────────────────────────────────────────────
    ("guide", "(front matter)", 0): ("f36ddedf7d0de988", "audience + pointer to the contract"),
    ("guide", "blocker", 0): ("fcecf71459ea67c0", "what this guide does and does not cover"),
    ("guide", "blocker", 1): ("06389181dce3477d", "who maintains the code and cuts releases"),
    ("guide", "blocker", 2): ("04ea3b2467e78210", "the three in-release references"),
    ("guide", "processes", 0): ("7fcf2738c2096c61", "how the image is built"),
    ("guide", "processes", 1): ("e8b1fd6efdad4dd6", "disable the HTTP healthcheck on workers"),
    ("guide", "configuration", 0): ("b69bc9148329c468", "where the full config sample lives"),
    ("guide", "releases", 0): ("fae3434680bd1512", "the rolling-release shape"),
    ("guide", "day2", 0): ("22a27d96c28fd96c", "three limits worth knowing before an incident"),
    ("guide", "day2", 1): ("1ca5e9968b5c4a90", "the two crons"),
}

DOC_NAMES = {id(contract_gen): "contract", id(deploy_gen): "guide"}


def _narration_digest(block) -> str:
    """Over the block's exact rendered content — lines AND table cells.

    Covering only `lines` would leave structural TABLES unpinned, and the contract's go-live
    checklist is exactly that: a table with no claim behind it whose every word is an obligation.
    """
    parts = [block.kind, *block.lines]
    for row in block.rows:
        parts.extend(row)
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def _narration(doc, name: str) -> dict:
    """Every unattributed, non-heading block, keyed by (document, section, ordinal)."""
    found, counter = {}, Counter()
    for section in doc.sections:
        for block in section.blocks:
            if block.claim_id is not None or block.kind == "heading":
                continue
            key = (name, section.section_id, counter[section.section_id])
            counter[section.section_id] += 1
            found[key] = block
    return found


def test_unattributed_prose_is_a_closed_pinned_set(rendered):
    generator, _registry, doc, _page, _tables = rendered
    name = DOC_NAMES[id(generator)]
    found = _narration(doc, name)
    listed = {k: v for k, v in NARRATION_LABELS.items() if k[0] == name}

    added = sorted(set(found) - set(listed))
    assert not added, (
        f"unattributed prose that nobody reviewed: {added}\n"
        "A block carrying no claim id publishes a fact nothing in the registry verifies. Either "
        "attach it to a claim, or add it to NARRATION_LABELS with its digest — which is the act "
        "of reviewing it."
    )
    removed = sorted(set(listed) - set(found))
    assert not removed, (
        f"NARRATION_LABELS still pins prose that is no longer rendered: {removed}. A stale entry "
        "silently widens the allowlist for whatever takes its place."
    )
    for key, block in found.items():
        pinned, label = listed[key]
        actual = _narration_digest(block)
        assert actual == pinned, (
            f"{key} ({label}) changed since it was reviewed.\n"
            f"  reviewed: {pinned}\n  now:      {actual}\n"
            f"  text:     {' '.join(block.lines)[:160]!r}\n"
            "Read the new text, then re-pin it in the SAME commit."
        )


def test_every_narration_entry_carries_a_human_label():
    """A digest alone tells the next reviewer nothing about what they are approving."""
    for key, (pinned, label) in NARRATION_LABELS.items():
        assert len(pinned) == 16 and re.fullmatch(r"[0-9a-f]{16}", pinned), key
        assert len(label.strip()) >= 12, f"{key} has no usable label"


def test_appending_unattributed_prose_fails_the_closed_set(tmp_path):
    """The F3 attack, run against this control: a contradictory paragraph that belongs to no
    claim. It used to need `exclusive_terms` to name its subject in advance; now it fails simply
    for being unlisted."""
    doc = _build(contract_gen)
    doc.section("smuggled", "Appendix")
    doc.p("We commit the decision before returning 2xx, so a duplicate cannot occur.")
    with pytest.raises(AssertionError, match="nobody reviewed"):
        test_unattributed_prose_is_a_closed_pinned_set(
            (contract_gen, WIRE, doc, "", []))


def test_editing_reviewed_prose_fails_the_pin(monkeypatch):
    """Rewriting narration in place keeps the key and changes the meaning — the mutation a
    key-only allowlist waves through."""
    doc = _build(deploy_gen)
    found = _narration(doc, "guide")
    key = ("guide", "releases", 0)
    original = found[key]
    tampered = dataclasses.replace(
        original,
        lines=("Rolling release: pull the tag and restart everything at once; no drain needed.",),
    )
    assert _narration_digest(tampered) != NARRATION_LABELS[key][0]


def test_the_page_uses_no_vocabulary_the_model_does_not_carry(rendered):
    """The other direction: text on the page that the model has never heard of.

    The pinned set above governs what the GENERATOR declares. This governs what the PAGE actually
    carries, so a renderer that draws its own words — a hardcoded string in a style, a stamped
    label, a template leaking through — is caught even though no block records it. Words are the
    unit because layout is not: reportlab wraps prose and pdfplumber interleaves table columns, so
    any longer span is an artifact of typesetting rather than of content.
    """
    generator, _registry, doc, page, _tables = rendered
    known = set()
    for section in doc.sections:
        known.update(_normalize(w) for w in section.title.split())
        for block in section.blocks:
            for line in block.lines:
                known.update(_normalize(w) for w in line.split())
            for row in block.rows:
                for cell in row:
                    known.update(_normalize(w) for w in cell.split())
    # Wrapping splits a token across two lines, so each half is a prefix or suffix of a known
    # word rather than a word. Accept only that, and only against the words actually present.
    unknown = []
    for word in page.split():
        needle = _normalize(word)
        if not needle or needle in known:
            continue
        if any(k.startswith(needle) or k.endswith(needle) for k in known):
            continue
        unknown.append(word)
    assert not unknown, (
        f"{DOC_NAMES[id(generator)]}: the page carries words the document model does not: "
        f"{sorted(set(unknown))[:20]}"
    )


# ── labels the renderer authors ───────────────────────────────────────────────────────────────
#
# `claim_paragraph(prefix=...)` frames a claim's value for the reader: "Compliance window (days):
# 2555". The value is the registry's and is checked against its authority; the LABEL is the
# renderer's and has no authority to be checked against. It was also, until this commit, not
# recorded in the block at all — so it reached the page while the model denied drawing it, and
# rewriting "(days)" to "(years)" published a false statement with the whole suite green.
#
# Recording it fixes the under-description. Pinning it here fixes the rest: a label that the model
# and the page agree on is still only the renderer agreeing with itself.

CLAIM_LABELS = {
    ("contract", "WIRE.INGEST.PATH"): "Endpoint:",
    ("contract", "WIRE.CALLBACK.PATH"): "Endpoint:",
    ("contract", "WIRE.SIGN.SKEW_SECONDS"): "Skew window (seconds):",
    ("contract", "WIRE.SIGN.V1_SUNSET"): "On the v1 sunset dates:",
    ("contract", "WIRE.ORDERING.INTERIM"): "Until activation:",
    ("contract", "WIRE.ORDERING.INTEGRITY_MISMATCH"): "Note:",
    ("contract", "WIRE.RETENTION.WINDOW_DAYS"): "Compliance window (days):",
    ("guide", "OPS.CONFIG.ROTATION_KEYS"): "Rotation keys:",
}


def _claim_labels(doc, name: str) -> dict:
    """Every block whose recorded lines carry a label ahead of the projected value."""
    out = {}
    for block in doc.blocks:
        if not block.claim_id or block.projection != projection.PARAGRAPH:
            continue
        derived = projection.expected_lines(doc.registry[block.claim_id], block.projection)
        extra = [line for line in block.lines if line not in derived]
        if extra:
            out[(name, block.claim_id)] = " ".join(extra)
    return out


def test_renderer_authored_labels_are_pinned(rendered):
    generator, _registry, doc, page, _tables = rendered
    name = DOC_NAMES[id(generator)]
    found = _claim_labels(doc, name)
    listed = {k: v for k, v in CLAIM_LABELS.items() if k[0] == name}
    assert set(found) == set(listed), (
        f"labels added or removed without review: added={sorted(set(found) - set(listed))} "
        f"removed={sorted(set(listed) - set(found))}"
    )
    for key, text in found.items():
        assert text == listed[key], (
            f"{key}: the label was reworded.\n  reviewed: {listed[key]!r}\n  now:      {text!r}\n"
            "A label frames the value beside it; re-read it and re-pin in the SAME commit."
        )
        # and it is genuinely on the page, immediately before the value it frames
        assert _flat(text) in page, f"{key}: the pinned label is not on the page"


def test_a_reworded_label_fails_the_pin(monkeypatch, tmp_path):
    """The 'days' -> 'years' mutation, run end to end.

    Before the label was recorded this changed the page and nothing else — no model line moved, no
    claim value changed, and the value beside it stayed correct. It is the cheapest way to make an
    externally-binding document say something false.
    """
    from docs.generators import render as render_module

    original = render_module.Doc.claim_paragraph

    def relabelled(self, cid, *, style=render_module.BODY, prefix=""):
        if cid == "WIRE.RETENTION.WINDOW_DAYS":
            prefix = "<b>Compliance window (years): </b>"
        return original(self, cid, style=style, prefix=prefix)

    monkeypatch.setattr(render_module.Doc, "claim_paragraph", relabelled)
    doc = _build(contract_gen)
    path = str(tmp_path / "relabelled.pdf")
    doc.build(path)
    page = body_text(path)

    assert "Compliance window (years): 2555" in page, "the mutation did not reach the page"
    with pytest.raises(AssertionError, match="reworded"):
        test_renderer_authored_labels_are_pinned((contract_gen, WIRE, doc, page, []))


def test_the_vocabulary_check_catches_a_word_the_model_never_recorded(monkeypatch, tmp_path):
    """Negative control for the page->model direction: text drawn straight onto the page, with no
    block behind it at all. This is the shape a stamped label or a leaked template takes, and the
    model-side checks cannot see it because the model does not know it exists."""
    from docs.generators import render as render_module

    original = render_module.Doc.p

    def also_draw_unrecorded(self, markup, style=render_module.BODY):
        original(self, markup, style)
        # appended to the story, deliberately NOT to any block
        self._story.append(render_module.Paragraph("Superseding addendum: zzyzx.", style))
        render_module.Doc.p = original  # once is enough

    monkeypatch.setattr(render_module.Doc, "p", also_draw_unrecorded)
    doc = _build(deploy_gen)
    monkeypatch.setattr(render_module.Doc, "p", original)
    path = str(tmp_path / "unrecorded.pdf")
    doc.build(path)
    page = body_text(path)

    assert "zzyzx" in page, "the unrecorded paragraph did not reach the page"
    with pytest.raises(AssertionError, match="does not"):
        test_the_page_uses_no_vocabulary_the_model_does_not_carry(
            (deploy_gen, OPERATIONS, doc, page, []))


# ── prose the renderer authors INSIDE a claim's block ─────────────────────────────────────────
#
# Self-audit of the two commits above. Pinning unattributed BLOCKS closed one door and left the
# adjacent one open: a block that carries a claim id is exempt from the narration pin, and
# `CLAIM_LABELS` only covers `claim_paragraph` prefixes. Everything a composed or tabular block
# says AROUND its claim's values — connective sentences, column headers, "Trap:", "Reversible?" —
# was renderer-authored, recorded (so the vocabulary check passes), attributed (so the narration
# pin skips it), and pinned by nothing.
#
# That is not a decorative surface. `WIRE.INGEST.EXTRA_FIELDS` promises a counterparty in prose
# that we "add without notice and never remove or repurpose one without a version bump agreed with
# you" — a binding forward-compatibility commitment with no claim behind it.
#
# So the rule becomes uniform, and it is the span-equality property finding 3 actually asked for:
# EVERY character on the page is either derived from the registry or pinned here.

RENDERER_PROSE = {
    # Table blocks no longer appear here at all (Wave 2 F5): their column titles come from
    # Claim.columns and every cell from the projection, so a claim table carries no
    # renderer-authored words to pin.
    # ── contract ──────────────────────────────────────────────────────────────────────────────
    ("contract", "WIRE.INGEST.EXTRA_FIELDS", 0): ("3dbd54f03ecebb08",
                                                  "the forward-compatibility commitment: we add "
                                                  "without notice, never remove or repurpose "
                                                  "without an agreed version bump"),
    ("contract", "WIRE.ACTOR.SENSITIVE", 0): ("841eed537d25b0f4",
                                              "actor.type/actor.id equality rule and its trap"),
    ("contract", "WIRE.CALLBACK.FIELDS", 0): ("fd2b8fddb0219846", "required body fields lead-in"),
    ("contract", "WIRE.CALLBACK.DECISIONS", 0): ("efd06031f9154b80", "decision enumeration lead-in"),
    ("contract", "WIRE.CALLBACK.GATES", 0): ("60ff773c179a76ed", "gates enumeration lead-in"),
    ("contract", "WIRE.CALLBACK.OPTIONAL_FIELDS", 0): ("adcaa34e853e35fc",
                                                       "the optional-field lead-in; the handling "
                                                       "RULE moved to its own statement claim "
                                                       "(Wave-2 finding 1)"),
    ("contract", "WIRE.CALLBACK.RETRY", 0): ("e07949a7ee8f8121",
                                             "retry schedule lead-in and totals"),
    ("contract", "WIRE.SIGN.CANONICAL", 0): ("44258b84c7d9f360", "canonical-string lead-in"),
    ("contract", "WIRE.SIGN.DIRECTIONS", 0): ("3db54252a0da969e",
                                              "direction tokens are literals; what slot and "
                                              "path?query are (the prose-will-not-verify RULE "
                                              "moved to its own statement claim, Wave-2 "
                                              "finding 1)"),
    ("contract", "WIRE.SIGN.COMPANION", 0): ("62bed443d66d463f", "companion filename + sha256 line"),
    ("contract", "WIRE.SIGN.VECTOR", 0): ("723d2cc6883b5453", "worked vector lead-in"),
    # ── guide ─────────────────────────────────────────────────────────────────────────────────
    ("guide", "OPS.CONFIG.HMAC_SET", 0): ("eeead37d4b6834fd", "the HMAC set lead-in"),
    ("guide", "OPS.CUTOVER.PROCEDURES", 0): ("33b3260daf6c3302",
                                             "per-procedure framing: what must be true before "
                                             "you start, Reversible?, Rolling back, Playbook"),
    ("guide", "OPS.CUTOVER.OUTBOX_CEILING", 0): ("1193b2813e8d6c5b", "ceiling cutover heading"),
    ("guide", "OPS.HMAC.ROLLOUT_ORDER", 0): ("b533fa723b3b1f48", "rollout order heading"),
}

# Below this, a block's residue is separators and numbering rather than words.
CONNECTIVE_FLOOR = 6


def _connective_text(block, claim) -> str:
    """What the block draws, minus everything the CLAIM supplies.

    Longest leaf first: `approve` is a substring of `approve_buy_locked`, and removing the short
    one first would leave `_buy_locked` behind and make the residue depend on iteration order.
    """
    parts = list(block.lines)
    for row in block.rows:
        parts.extend(row)
    text = "\n".join(parts)
    # column titles are claim-supplied too (Claim.columns, Wave 2 F5), so they are not
    # the renderer's words any more than the cells are
    leaves = sorted(
        (leaf for leaf in (*projection.leaf_strings(claim.value, block.row_fields),
                           *claim.headers) if leaf),
        key=len, reverse=True,
    )
    for leaf in leaves:
        text = text.replace(leaf, "\x00")

    return re.sub(r"\x00+", "\x00", text)


def _renderer_prose(doc, name: str) -> dict:
    found, seen = {}, Counter()
    for block in doc.blocks:
        # PARAGRAPH prefixes are pinned by exact wording in CLAIM_LABELS, which is stricter and
        # more readable than a digest; pinning them twice would just mean two places to update.
        if not block.claim_id or block.projection == projection.PARAGRAPH:
            continue
        text = _connective_text(block, doc.registry[block.claim_id])
        if len(_normalize(text)) <= CONNECTIVE_FLOOR:
            continue
        key = (name, block.claim_id, seen[block.claim_id])
        seen[block.claim_id] += 1
        found[key] = text
    return found


def test_prose_the_renderer_authors_inside_a_claim_is_pinned(rendered):
    generator, _registry, doc, _page, _tables = rendered
    name = DOC_NAMES[id(generator)]
    found = _renderer_prose(doc, name)
    listed = {k: v for k, v in RENDERER_PROSE.items() if k[0] == name}

    added = sorted(set(found) - set(listed))
    assert not added, (
        f"the renderer authors prose nobody reviewed: {added}\n"
        "A claim id on the block does not make the words around the claim's values authoritative "
        "— they are the renderer's. Pin them here, which is the act of reviewing them."
    )
    removed = sorted(set(listed) - set(found))
    assert not removed, f"RENDERER_PROSE pins prose no longer rendered: {removed}"
    for key, text in found.items():
        pinned, label = listed[key]
        actual = hashlib.sha256(text.encode()).hexdigest()[:16]
        assert actual == pinned, (
            f"{key} ({label}) changed since it was reviewed.\n"
            f"  reviewed: {pinned}\n  now:      {actual}\n"
            f"  text:     {' '.join(text.replace(chr(0), '~').split())[:200]!r}\n"
            "Read it, then re-pin in the SAME commit."
        )


def test_rewriting_a_binding_commitment_in_connective_prose_is_caught(monkeypatch, tmp_path):
    """The finding, run as an attack.

    `WIRE.INGEST.EXTRA_FIELDS` promises we never repurpose a callback field without an agreed
    version bump. That sentence is the generator's, not the registry's, so before this pin it
    could be reversed outright: the claim's values still rendered, the block still carried its
    claim id, the vocabulary check still passed because every word already appears on the page.
    """
    from docs.generators import render as render_module

    original = render_module.Doc.claim_prose

    def weakened(self, cid, markup, *, style=render_module.BODY):
        if cid == "WIRE.INGEST.EXTRA_FIELDS":
            markup = markup.replace(
                "never remove or repurpose one without a version bump agreed with you",
                "may remove or repurpose one at any time")
        return original(self, cid, markup, style=style)

    monkeypatch.setattr(render_module.Doc, "claim_prose", weakened)
    doc = _build(contract_gen)

    found = _renderer_prose(doc, "contract")
    key = ("contract", "WIRE.INGEST.EXTRA_FIELDS", 0)
    assert "may remove or repurpose one at any time" in found[key], "the mutation did not land"
    with pytest.raises(AssertionError, match="changed since it was reviewed"):
        test_prose_the_renderer_authors_inside_a_claim_is_pinned(
            (contract_gen, WIRE, doc, "", []))


def test_the_connective_residue_is_stable_under_overlapping_leaf_values(rendered):
    """`approve` is a substring of `approve_buy_locked`. If leaves were removed shortest-first the
    residue would keep `_buy_locked` and the digest would depend on iteration order rather than on
    what the renderer wrote."""
    _generator, registry, doc, _page, _tables = rendered
    for block in doc.blocks:
        if not block.claim_id or block.projection == projection.PARAGRAPH:
            continue
        claim = registry[block.claim_id]
        first = _connective_text(block, claim)
        assert _connective_text(block, claim) == first
        leaves = [x for x in projection.leaf_strings(claim.value, block.row_fields) if x]
        for leaf in leaves:
            assert leaf not in first, (
                f"{block.claim_id}: the claim value {leaf[:40]!r} survived into the residue, so "
                "the pin would cover registry-owned content and fail on a legitimate claim edit"
            )


# ── Wave-1 gate F10: the answer table may not contradict the resolution gate ──────────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 10. The generator said answers are "listed below against
# the obligation each one clears" and headed a column "What it unblocks", while the claim beside
# it correctly said no reply can resolve anything until the signed artifact schema ships — the
# counterparty received mutually exclusive instructions.


def test_f10_gate_the_unresolvable_alert_precedes_the_answer_table():
    """The PENDING note (which carries the no-reply-can-resolve statement) must be VISIBLE before
    any answer row, and the deliverable column must be negated, not affirmative."""
    doc = _build(contract_gen)
    blocks = doc.blocks
    # the no-reply-can-resolve sentence is its own claim now (Wave-2 finding 1), so the
    # ordering is compared across the two claims rather than within one
    statement_at = next(
        i for i, b in enumerate(blocks) if b.claim_id == "WIRE.ORDERING.OBLIGATION_STATE")
    table_at = next(
        i for i, b in enumerate(blocks)
        if b.claim_id == "WIRE.ORDERING.PENDING_INPUTS" and b.kind == "table")
    assert statement_at < table_at, (
        "the resolution-impossible statement renders after the answer rows")
    header_row = blocks[table_at].rows[0]
    assert any("does not unblock" in cell for cell in header_row), header_row
    assert not any(cell == "What it unblocks" for cell in header_row), (
        "the affirmative header is back"
    )


def test_f10_gate_no_affirmative_clearing_language_in_the_asks_section():
    """The framing prose may not promise that an answer CLEARS an obligation while the gate says
    resolution is impossible; the screened-but-cannot-clear wording is the honest form."""
    doc = _build(contract_gen)
    asks = next(s for s in doc.sections if s.section_id == "asks")
    prose = " ".join(" ".join(b.lines) for b in asks.blocks)
    assert "each one clears" not in prose
    assert "screened" in prose and "cannot clear" in prose
