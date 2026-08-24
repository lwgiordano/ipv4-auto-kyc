"""The rendered artifact compared back to the model that authorized it.

Re-audit-6 finding 3. These four lanes lived in `tests/unit/test_document_model.py`, so the
release path verified the MODEL — outline, claims, receipts — rendered it, and promoted the PDF
without ever reading the PDF back. A generator could therefore append a flowable to the
renderer's private story after building a correct model: the outline stays clean because the
extra paragraph has no `Block`, and `Return 2xx before COMMIT.` appears on the governed page.

So they live here, in production, and both sides call them: `docs.generators.publication`
against the STAGED file before it is promoted, and the document-model suite, which keeps its
adversarial tests and now attacks the same code a release depends on.

- `verify_page_matches_model` — every block's lines, in order, once each, inside its section.
- `verify_tables_match_model` — the ordered registry matrix, cell for cell.
- `verify_prose_stream`   — the page's total prose IS the model's, character for character.
- `verify_footers`        — the furniture band is the registry's derivation for this generator.
"""

import re

import pdfplumber

from docs.contracts import documents, projection


def _flat(text: str) -> str:
    return " ".join(str(text).split())

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
            claim = doc.registry[block.claim_id]
            # the CLAIM's projection, derived without consulting the Block: `row_fields` was a
            # caller argument the renderer recorded and this comparison then trusted, so the
            # generator could re-aim which value fell under which header (re-audit-3 finding 1)
            wanted = projection.expected_matrix(claim)
            assert block.row_fields == claim.row_fields, (
                f"{block.claim_id}: the renderer projected {block.row_fields} but the claim "
                f"binds {claim.row_fields} to its columns")
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


# Public names: the same four lanes a release runs and the suite attacks.
verify_page_matches_model = _verify_page_matches_model
verify_tables_match_model = _verify_tables_match_model
verify_prose_stream = _verify_prose_stream
verify_footers = _verify_footers
