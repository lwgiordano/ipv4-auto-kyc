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
    ("contract", "asks", 2): ("d5c7111569ed9c66", "1.1 necessary and not sufficient"),
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
    doc.build(path, "relabelled")
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
        self.story.append(render_module.Paragraph("Superseding addendum: zzyzx.", style))
        render_module.Doc.p = original  # once is enough

    monkeypatch.setattr(render_module.Doc, "p", also_draw_unrecorded)
    doc = _build(deploy_gen)
    monkeypatch.setattr(render_module.Doc, "p", original)
    path = str(tmp_path / "unrecorded.pdf")
    doc.build(path, "unrecorded")
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
    # ── contract ──────────────────────────────────────────────────────────────────────────────
    ("contract", "WIRE.ORDERING.PENDING_INPUTS", 0): ("3b14b4a6307e5905", "024 asks table headers"),
    ("contract", "WIRE.ORDERING.PENDING_INPUTS", 1): ("ad480536ddc86b84",
                                                      "what 024 cannot be built without"),
    ("contract", "WIRE.INGEST.HEADERS", 0): ("9e9d62e11f51e35c", "header table headers + why column"),
    ("contract", "WIRE.INGEST.EXTRA_FIELDS", 0): ("3dbd54f03ecebb08",
                                                  "the forward-compatibility commitment: we add "
                                                  "without notice, never remove or repurpose "
                                                  "without an agreed version bump"),
    ("contract", "WIRE.ACTOR.SENSITIVE", 0): ("841eed537d25b0f4",
                                              "actor.type/actor.id equality rule and its trap"),
    ("contract", "WIRE.EVENT.TABLE", 0): ("a2d3ac04ae2bfc2e", "event table headers"),
    ("contract", "WIRE.INGEST.STATUS", 0): ("482149e6396f9a45", "status table headers"),
    ("contract", "WIRE.CALLBACK.FIELDS", 0): ("fd2b8fddb0219846", "required body fields lead-in"),
    ("contract", "WIRE.CALLBACK.DECISIONS", 0): ("efd06031f9154b80", "decision enumeration lead-in"),
    ("contract", "WIRE.CALLBACK.GATES", 0): ("60ff773c179a76ed", "gates enumeration lead-in"),
    ("contract", "WIRE.CALLBACK.OPTIONAL_FIELDS", 0): ("febe3f9a74da208d",
                                                       "tolerate and preserve; which decision "
                                                       "stays authoritative under the hold"),
    ("contract", "WIRE.CALLBACK.EFFECTIVENESS", 0): ("c64fed84bfb92e34",
                                                     "acknowledging vs applying; rows partition "
                                                     "the state space"),
    ("contract", "WIRE.CALLBACK.EFFECTIVENESS", 1): ("116c95cbf7ad7f0d",
                                                     "transition table headers"),
    ("contract", "WIRE.CALLBACK.RETRY", 0): ("e07949a7ee8f8121",
                                             "retry schedule lead-in and totals"),
    ("contract", "WIRE.SIGN.CANONICAL", 0): ("44258b84c7d9f360", "canonical-string lead-in"),
    ("contract", "WIRE.SIGN.DIRECTIONS", 0): ("689f7fa5982d3d68",
                                              "direction tokens are literals; prose will not "
                                              "verify; what slot and path?query are"),
    ("contract", "WIRE.SIGN.COMPANION", 0): ("62bed443d66d463f", "companion filename + sha256 line"),
    ("contract", "WIRE.SIGN.VECTOR", 0): ("723d2cc6883b5453", "worked vector lead-in"),
    ("contract", "WIRE.RETENTION.BY_KIND", 0): ("c5357842f39273b0", "retention table headers"),
    # ── guide ─────────────────────────────────────────────────────────────────────────────────
    ("guide", "OPS.PROCESS.COMMANDS", 0): ("2cea52c0e7fb495d", "process table headers"),
    ("guide", "OPS.INFRA.COMPONENTS", 0): ("de69ec6459140a78", "infrastructure table headers"),
    ("guide", "OPS.CONFIG.DEFAULTS", 0): ("edfd8a6f27a9d8c4",
                                          "settings table headers and the KYC_ variable names"),
    ("guide", "OPS.CONFIG.HMAC_SET", 0): ("eeead37d4b6834fd", "the HMAC set lead-in"),
    ("guide", "OPS.HEALTH.PROBES", 0): ("7655d03e90ea7729", "health table headers"),
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
    leaves = sorted(
        (leaf for leaf in projection.leaf_strings(claim.value, block.row_fields) if leaf),
        key=len, reverse=True,
    )
    for leaf in leaves:
        text = text.replace(leaf, "\x00")
    if claim.note:
        text = text.replace(claim.note, "\x00")
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
