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


# ── the glyphs themselves: governed text must be VISIBLE, not merely extractable ──────────────────
#
# Re-audit-10 finding 2, completed by re-audit-11 finding 1. Every lane above reads the page
# through extraction, and extraction does not care what the ink looks like: the retry obligation
# rendered in white passed the outline, the page/model comparison, the table, prose and footer
# lanes, and published — complete to a parser, absent to the person the contract binds.
#
# Two lanes, because declared ink and painted ink are different facts:
#
# - `visibility_problems` reads each character's DECLARED colour, size and position from the
#   content stream. It is cheap and it names the defect precisely, and re-audit-11 proved it is
#   not an authority: alpha-0 ink still declares black, black text on a black table background
#   declares nothing wrong, and an opaque shape painted over a finished page changes no
#   character at all.
# - `painted_problems` is the authority, and it measures CONTRIBUTION, not contrast
#   (re-audit-12 finding 1). The first painted lane accepted a character when its box held both
#   light and dark pixels — which proves something is painted there, not that the GLYPH is: a
#   1pt checkerboard stamped over a finished page gave every box maximal contrast while no word
#   on it was readable. So each page is now rasterized twice with the same renderer — once as
#   published, once with every text object removed — and each character's box must show a
#   visible DIFFERENCE between the two: the ink the text layer itself leaves on the finished
#   page, after opacity, render mode, clipping, backgrounds and anything painted later.
#
# Robustness is by construction, not by tuning (re-audit-12 finding 2): a tight metric box under
# a fixed threshold flipped with the platform's rasterizer — the unmodified document failed its
# own baseline on another machine over an underscore hugging the box's bottom edge, and measured
# 0.00 here at a coarser scale. The measured quantity is now |with text − without| — the glyph's
# own ink against its own ground, wherever anti-aliasing lands the stroke — the box is padded
# below for descender-hugging glyphs, the scale gives the thinnest governed stroke a full pixel,
# and the real documents are additionally held to MINIMUM_REAL_CONTRIBUTION, so a drifting
# environment fails the guard loudly while the release floor still holds with headroom.
#
# Scope, stated honestly: the box is padded, so a neighbouring glyph's ink can bleed into a
# character's window — the granularity is the padded box, not the lone glyph. And contribution
# proves the glyph lands visibly against ITS ground; a ground made deliberately noisy could
# degrade legibility without erasing contribution — bounded by the role closure over governed
# grounds, not measured. Text nested where the removal walk cannot reach (form XObjects —
# nothing this renderer emits) stays in both rasters, measures zero, and is REFUSED.

MINIMUM_GLYPH_POINTS = 6.0  # the smallest governed role is the 7pt footer
MAXIMUM_INK_LUMINANCE = 0.75  # against the white page; the palest governed ink is #666666 (~0.40)
RASTER_DPI = 200  # the thinnest governed stroke (a 7pt em-dash, ~0.35pt) is ~a full pixel here
MINIMUM_PAINTED_CONTRAST = 0.25  # 1 − MAXIMUM_INK_LUMINANCE: the same floor, measured painted
# The drift guard the REAL documents are held to, sitting between the release floor and the
# palest governed ink at full coverage (#666666 on white contributes 0.60): an environment that
# renders governed hairlines below two-thirds of their ink is drifting toward the floor, and the
# guard fails loudly there while readers are still 0.15 of headroom away from a wrong refusal.
MINIMUM_REAL_CONTRIBUTION = 0.40


def _luminance(color) -> float:
    """Perceived luminance of a pdfplumber char colour, 0 (black) to 1 (white)."""
    if color is None:
        return 0.0  # the default colour space paints black
    values = list(color) if isinstance(color, (list, tuple)) else [color]
    if len(values) == 1:
        gray = float(values[0])
        return gray
    if len(values) == 3:
        r, g, b = (float(v) for v in values)
        return 0.299 * r + 0.587 * g + 0.114 * b
    if len(values) == 4:
        c, m, y, k = (float(v) for v in values)
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
        return 0.299 * r + 0.587 * g + 0.114 * b
    return 0.0


def visibility_problems(path: str) -> list[str]:
    """Every way a glyph in `path` fails to be readable ink on the visible page."""
    found: list[str] = []
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            for char in page.chars:
                text = (char.get("text") or "").strip()
                if not text:
                    continue
                luminance = _luminance(char.get("non_stroking_color"))
                if luminance > MAXIMUM_INK_LUMINANCE:
                    found.append(
                        f"page {number}: {text!r} is drawn in ink of luminance "
                        f"{luminance:.2f} on a white page — extractable, invisible")
                elif char.get("size", 0) < MINIMUM_GLYPH_POINTS:
                    found.append(
                        f"page {number}: {text!r} is drawn at {char.get('size'):.1f}pt, below "
                        f"the governed minimum of {MINIMUM_GLYPH_POINTS}pt")
                elif (char["x0"] < 0 or char["x1"] > page.width
                      or char["top"] < 0 or char["bottom"] > page.height):
                    found.append(
                        f"page {number}: {text!r} is drawn outside the visible page")
                if len(found) >= 10:
                    return found
    return found


def text_contributions(path: str):
    """Yield (page, character, contribution) for every extractable character in `path`.

    Contribution is the largest painted difference, 0.0..1.0, between the finished page and the
    same page with its text layer removed, within the character's padded box — the ink the text
    itself leaves on the page a reader holds, measured AFTER everything else has painted. A glyph
    erased by alpha, drowned by its own background, or covered by later paint contributes
    nothing; a glyph that lands contributes ~its ink-to-ground difference wherever the
    rasterizer put its stroke, which is what makes the measure stable across renderer builds.

    Both rasters come from the same renderer at the same scale, so they differ ONLY by the text
    objects removed in between. The box is padded — 2px around, more below, because descender
    glyphs like `_` hug or cross the metric box's bottom edge and a tight crop turned platform
    rounding into verdicts (re-audit-12 finding 2).
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    from PIL import ImageChops

    scale = RASTER_DPI / 72.0
    inked = pdfium.PdfDocument(path)
    blank = pdfium.PdfDocument(path)
    try:
        with pdfplumber.open(path) as pdf:
            for index, page in enumerate(pdf.pages):
                page_a, page_b = inked[index], blank[index]
                image_a = page_a.render(scale=scale).to_pil().convert("L")
                # collect first, then remove: removal renumbers the page's object list
                handles = [pdfium_c.FPDFPage_GetObject(page_b, position)
                           for position in range(pdfium_c.FPDFPage_CountObjects(page_b))]
                for handle in handles:
                    if (pdfium_c.FPDFPageObj_GetType(handle) == pdfium_c.FPDF_PAGEOBJ_TEXT
                            and pdfium_c.FPDFPage_RemoveObject(page_b, handle)):
                        pdfium_c.FPDFPageObj_Destroy(handle)
                pdfium_c.FPDFPage_GenerateContent(page_b)
                difference = ImageChops.difference(
                    image_a, page_b.render(scale=scale).to_pil().convert("L"))
                for char in page.chars:
                    text = (char.get("text") or "").strip()
                    if not text:
                        continue
                    descent = max(3, int(0.35 * (char["bottom"] - char["top"]) * scale))
                    left = max(0, int(char["x0"] * scale) - 2)
                    upper = max(0, int(char["top"] * scale) - 2)
                    right = min(difference.width, int(char["x1"] * scale) + 3)
                    lower = min(difference.height, int(char["bottom"] * scale) + descent)
                    if right <= left or lower <= upper:
                        yield index + 1, text, 0.0
                        continue
                    _, high = difference.crop((left, upper, right, lower)).getextrema()
                    yield index + 1, text, high / 255.0
    finally:
        blank.close()
        inked.close()


def painted_problems(path: str) -> list[str]:
    """Every character whose text layer leaves no visible mark on the finished page.

    Alpha-0 "black", black cells on a black table background, a page wiped by an opaque shape,
    and a page wiped and then TILED with a high-contrast pattern (re-audit-12 finding 1 — box
    contrast said that page was fine) all fail here: whatever else the box shows, removing the
    text changes nothing a reader could see, so the text was never visible.
    """
    found: list[str] = []
    for number, text, contribution in text_contributions(path):
        if contribution < MINIMUM_PAINTED_CONTRAST:
            found.append(
                f"page {number}: {text!r} contributes no visible ink to the finished page "
                f"(painted contribution {contribution:.2f}, floor {MINIMUM_PAINTED_CONTRAST})")
            if len(found) >= 10:
                break
    return found


# ── the ink each ROLE is allowed to wear: recorded role == painted look, per character ────────────
#
# Re-audit-11 finding 2, the page half. The reviewed outline pins which presentation role every
# block occupies, but the block's `roles` are recorded by the layer under audit — a generator
# that draws an audience paragraph as a red alert panel and records "BODY" tells the outline a
# clean story. The prose-stream lane already proves the page's prose characters EQUAL the model's
# characters in order, so the pairing between a painted character and the line that authorized it
# is exact: this lane walks the two streams in lockstep and holds each painted character to the
# closed signature of ITS line's role. Table cells and the footer band close the partition.


def _ink_rgb(color) -> tuple[float, float, float]:
    """A pdfplumber character colour as canonical rounded RGB, whatever space it was declared in."""
    if color is None:
        return (0.0, 0.0, 0.0)
    values = [float(v) for v in (color if isinstance(color, (list, tuple)) else [color])]
    if len(values) == 1:
        values = values * 3
    elif len(values) == 4:
        c, m, y, k = values
        values = [(1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
    return tuple(round(v, 4) for v in values[:3])


def _wears(char, signature) -> bool:
    """Whether this painted character matches one closed role signature exactly."""
    fonts, size, ink = signature
    return (char["fontname"] in fonts
            and abs(float(char.get("size", 0)) - size) < 0.05
            and _ink_rgb(char.get("non_stroking_color")) == ink)


def verify_role_ink(doc, path: str) -> None:
    """Every painted character wears the ink of the role its line was reviewed in.

    Three checks partition the page:

    1. PROSE, in lockstep: the model's prose stream (each character tagged with its line's
       recorded role) against the page's prose stream (each character carrying its painted font,
       size and colour). The streams are character-equal — `verify_prose_stream` holds that — so
       the pairing is positional, total, and floor-free: character N of the page was authorized
       by character N of the model, and must be painted in that line's role.
    2. TABLE cells: every character inside a table's box wears one of the three closed cell
       styles. No caller can name a cell style, so the closed set is the whole claim.
    3. FOOTER band: every character in the band wears the furniture's one look.
    """
    from docs.generators import render

    signatures = render.ROLE_SIGNATURES
    expected: list[tuple[str, str, str]] = []  # (character, role, its line, for the message)
    for block in doc.blocks:
        decorated = block.kind == "alert" or block.projection == projection.BULLETS
        for line, role in zip(block.lines, block.roles, strict=True):
            text = ("–" + str(line)) if decorated else str(line)
            for character in "".join(text.split()):
                expected.append((character, role, str(line)))

    cell_signatures = [signatures["CELL"], signatures["CELLB"], signatures["TOKEN"]]
    cursor = 0
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            cutoff = page.height - FOOTER_BAND_INCHES * 72
            boxes = [t.bbox for t in page.find_tables()]

            def in_table(word, boxes=boxes) -> bool:
                cx = (word["x0"] + word["x1"]) / 2
                cy = (word["top"] + word["bottom"]) / 2
                return any(x0 <= cx <= x1 and y0 <= cy <= y1 for (x0, y0, x1, y1) in boxes)

            # split words wherever the painted look changes, so each word wears ONE signature
            words = page.extract_words(
                extra_attrs=["fontname", "size", "non_stroking_color"])

            for word in sorted((w for w in words if w["top"] >= cutoff),
                               key=lambda w: w["x0"]):
                assert _wears(word, signatures["FOOTER"]), (
                    f"page {number}: footer-band text {word['text']!r} is painted "
                    f"({word['fontname']}, {word['size']:.1f}pt, "
                    f"{_ink_rgb(word.get('non_stroking_color'))}), not the furniture's one look")

            body = [w for w in words if w["top"] < cutoff]
            for word in (w for w in body if in_table(w)):
                assert any(_wears(word, s) for s in cell_signatures), (
                    f"page {number}: table cell text {word['text']!r} is painted "
                    f"({word['fontname']}, {word['size']:.1f}pt, "
                    f"{_ink_rgb(word.get('non_stroking_color'))}), not one of the three closed "
                    "cell styles")

            lines: dict[float, list] = {}
            for word in (w for w in body if not in_table(w)):
                lines.setdefault(round(word["top"], 1), []).append(word)
            for top in sorted(lines):
                for word in sorted(lines[top], key=lambda w: w["x0"]):
                    for character in word["text"]:
                        if not character.strip():
                            continue
                        assert cursor < len(expected), (
                            f"page {number}: prose runs past the model at {word['text']!r}")
                        wanted, role, line = expected[cursor]
                        assert character == wanted, (
                            f"page {number}: prose diverges from the model at character "
                            f"{cursor} ({character!r} vs {wanted!r}); run verify_prose_stream")
                        assert _wears(word, signatures[role]), (
                            f"page {number}: {word['text']!r} is painted "
                            f"({word['fontname']}, {word['size']:.1f}pt, "
                            f"{_ink_rgb(word.get('non_stroking_color'))}), which is not the "
                            f"look of {role!r}, the reviewed role of its line {line[:60]!r}")
                        cursor += 1
    assert cursor == len(expected), (
        f"the page paints {cursor} prose characters but the model records {len(expected)}")
