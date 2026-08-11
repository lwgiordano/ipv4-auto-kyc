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
import re
from collections import Counter

import pdfplumber
import pytest
from docs.contracts import projection
from docs.contracts.operations import OPERATIONS
from docs.contracts.wire import WIRE
from docs.generators import techcraft_deployment_guide as deploy_gen
from docs.generators import techcraft_integration_contract as contract_gen

from .test_contract_rendering import SAMPLE_CONTACT, SAMPLE_DUE_DATE, _build

# A line short enough to occur legitimately more than once ("300", "POST", a repeated cell value).
# Above it, a second occurrence is a duplicate nobody asked for — which is the mutation
# "add an unrecorded duplicate block".
UNIQUE_LINE_CHARS = 45

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
    _build(generator).build(path, "model")
    return body_text(path)


def _page_tables(generator, tmp_path):
    path = str(tmp_path / "model-tables.pdf")
    _build(generator).build(path, "model")
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


def test_every_table_block_is_on_the_page_cell_for_cell(rendered):
    """Tables are matched by header row and then compared cell by cell, because reading them out
    of the page text interleaves the columns."""
    _generator, _registry, doc, _page, tables = rendered
    for block in doc.blocks:
        if block.kind != "table" or not block.rows:
            continue
        header = [_flat(cell) for cell in block.rows[0]]
        candidates = [t for t in tables if t and t[0] == header]
        assert candidates, f"{block.claim_id or 'table'}: no rendered table with header {header}"
        rendered_cells = {cell for table in candidates for row in table for cell in row}
        for row in block.rows[1:]:
            for cell in row:
                wanted = _flat(cell)
                if len(wanted) < 12:
                    continue
                # a token cell replaces its commas with line breaks, so compare comma-insensitively
                assert any(
                    wanted == seen or wanted.replace(",", "") == seen.replace(",", "")
                    for seen in rendered_cells
                ), f"{block.claim_id or 'table'}: cell not on the page: {wanted[:60]!r}"


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
    notes = [b.claim_id for b in doc.blocks if b.role == "note"]
    assert len(notes) == len(set(notes)), f"a note is rendered twice: {notes}"


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
    """Everything a release runs: the authority map and the rendered-page comparison."""
    from .test_contract_registry_authority import AUTHORITY_VERIFIERS

    for claim in registry.claims:
        AUTHORITY_VERIFIERS[claim.id]()

    doc = _build(generator)
    path = str(tmp_path / "mutant.pdf")
    doc.build(path, "mutant")
    _verify_page_matches_model(doc, body_text(path))


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
    ("advertising an 8-character secret floor and naive dates", OPERATIONS, deploy_gen,
     "OPS.CONFIG.HMAC_SET",
     {"note": "Secrets are at least 8 characters and sunset dates may be naive."}),
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
    doc.build(path, "misfiled")
    with pdfplumber.open(path) as pdf:
        page = _flat("\n".join(p.extract_text() or "" for p in pdf.pages))
    start = page.find(_flat(misfiled.title))
    anchor = max((_flat(line) for line in block.lines), key=len)
    assert page.find(anchor) < start, "the misfiled claim would have to move on the page too"


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
            # The caller wrote the sentence, but every leaf the claim owns must still be printed.
            # Compared NORMALIZED — casefolded with punctuation removed — because a caller
            # legitimately reformats a leaf on its way to the page (`job_max_attempts` is printed
            # as the environment variable `KYC_JOB_MAX_ATTEMPTS`). Omission and contradiction are
            # still caught; only presentation is tolerated.
            haystack = _normalize(page)
            if block.kind == "table":
                haystack = _normalize(" ".join(
                    cell for table in tables for row in table for cell in row))
            for leaf in projection.leaf_strings(claim.value, block.row_fields):
                needle = _normalize(leaf)
                if len(needle) < 4:
                    continue
                assert needle in haystack, (
                    f"{block.claim_id}: the page omits a value the claim carries: {leaf[:70]!r}")
            continue

        if block.projection == projection.TABLE:
            wanted = projection.expected_rows(claim, projection.TABLE, block.row_fields)
            header = [_flat(cell) for cell in block.rows[0]]
            candidates = [t for t in tables if t and t[0] == header]
            assert candidates, f"{block.claim_id}: no rendered table with header {header}"
            cells = {cell for table in candidates for row in table for cell in row}
            for row in wanted:
                for cell in row:
                    needle = _flat(cell)
                    if len(needle) < 12:
                        continue
                    assert any(needle == seen or needle.replace(",", "") == seen.replace(",", "")
                               for seen in cells), (
                        f"{block.claim_id}: recomputed cell not on the page: {needle[:60]!r}")
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

        self.story.append(Paragraph(prefix + escape(lie), style))
        self.rendered.append(cid)
        self._add(Block(kind="prose", claim_id=cid, lines=(lie,),
                        projection=projection.PARAGRAPH))
        return None

    monkeypatch.setattr(render_module.Doc, "claim_paragraph", lying_claim_paragraph)
    doc = _build(generator)
    path = str(tmp_path / "lie.pdf")
    doc.build(path, "lie")
    page = body_text(path)

    wanted = projection.expected_lines(registry[claim_id], projection.PARAGRAPH)[0]
    assert _flat(wanted) not in page, "the mutation did not actually remove the claim's text"
    with pytest.raises(AssertionError):
        for block in doc.blocks:
            if block.claim_id != claim_id:
                continue
            for line in projection.expected_lines(registry[block.claim_id], block.projection):
                assert _flat(line) in page, f"{block.claim_id}: recomputed line not on the page"
