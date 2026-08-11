"""Shared reportlab scaffolding for the TechCraft documents.

The renderers pull every authoritative value from `docs.contracts`. Prose here explains; it never
restates a value the registry owns. `Doc.claim()` records which claim ids reached a flowable, so
`tests/unit/test_contract_rendering.py` can prove coverage structurally as well as by extracting
text from the built PDF.
"""

import os
import re
import subprocess
from dataclasses import dataclass

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)

from docs.contracts import projection

_styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1x", parent=_styles["Heading1"], fontSize=15, spaceBefore=16, spaceAfter=6,
                    textColor=colors.HexColor("#1a1a2e"))
H2 = ParagraphStyle("H2x", parent=_styles["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4,
                    textColor=colors.HexColor("#1a1a2e"))
BODY = ParagraphStyle("Bodyx", parent=_styles["Normal"], fontSize=9.5, leading=13, spaceAfter=5)
WHY = ParagraphStyle("Why", parent=BODY, leftIndent=10, textColor=colors.HexColor("#444444"),
                     fontSize=9, leading=12, splitLongWords=0)
CODE = ParagraphStyle("Code", parent=_styles["Code"], fontSize=8, leading=10.5, leftIndent=8,
                      spaceAfter=5, backColor=colors.HexColor("#f4f4f4"))
# splitLongWords=0: prose in a narrow column still wraps, but a long identifier moves to the next
# line WHOLE instead of being cut in half. A cell rendered `BLOCKE` / `D_NO_AUTHORITATIVE_MAPPING`
# publishes a sentinel nobody can grep for.
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.5, leading=11, spaceAfter=0, splitLongWords=0)
CELLB = ParagraphStyle("CellB", parent=CELL, fontName="Helvetica-Bold")
ALERT = ParagraphStyle("Alert", parent=BODY, fontSize=10, leading=14, spaceAfter=6,
                       textColor=colors.HexColor("#7a1010"), backColor=colors.HexColor("#fdf0f0"),
                       borderPadding=6, leftIndent=2, rightIndent=2)

_TABLE_STYLE = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8f0")),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
])


TOKEN = ParagraphStyle("Token", parent=CELL, fontName="Courier", fontSize=7.6, leading=10,
                       splitLongWords=0, wordWrap=None)
STEP = ParagraphStyle("Step", parent=BODY, leftIndent=18, firstLineIndent=-14, spaceAfter=3)
# Fixed-width text that MUST wrap: wordWrap="CJK" breaks between characters, which is the only way
# a 163-byte JSON body with no spaces fits a page at all.
WRAPCODE = ParagraphStyle("WrapCode", parent=CODE, wordWrap="CJK", splitLongWords=1)

# Usable text width: letter minus the 0.75in margins, minus CODE's left indent.
FRAME_WIDTH = letter[0] - 2 * 0.75 * inch
_CODE_ROOM = FRAME_WIDTH - CODE.leftIndent


def _guard_preformatted(text: str) -> str:
    """XPreformatted does NOT wrap and does NOT honour `<br/>`.

    Both were live defects. Joining steps with `<br/>` produced one run-on line (the tag is
    silently dropped), and that line then ran off the right edge of the page — the receiver's
    commit-before-2xx contract, the single most important requirement in the integration document,
    was printed unreadable. Extraction tests did not catch it because pdfplumber happily reports
    glyphs positioned outside the page box.

    So: preformatted content is line-split on real newlines and every line must fit. Anything that
    needs to wrap belongs in `wrapcode()` or an ordinary paragraph.
    """
    if "<br/>" in text:
        raise ValueError("XPreformatted ignores <br/>; join preformatted lines with a newline")
    for line in text.split("\n"):
        width = stringWidth(line, CODE.fontName, CODE.fontSize)
        if width > _CODE_ROOM:
            raise ValueError(
                f"preformatted line needs {width:.0f}pt but the frame allows {_CODE_ROOM:.0f}pt, "
                f"so it would run off the page: {line[:60]!r}..."
            )
    return text


class ProvenanceError(RuntimeError):
    """A release build could not establish which commit it came from."""


def source_revision() -> str:
    """The commit these pages were rendered from, plus a dirty marker. Printed in the footer so a
    stale PDF is distinguishable from the audited one years later.

    Best-effort BY DESIGN, and that is why `build(release=True)` refuses what it returns here when
    it degrades: swallowing every failure means a git that is missing, broken, or simply not
    installed on the build host produces a release-looking PDF stamped "source unknown", which is
    exactly the artifact the footer exists to make impossible (re-audit `4f23f23..97deeae`
    finding 9)."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        ).stdout.strip()
        return f"{head}{'+dirty' if dirty else ''}"
    except Exception:  # noqa: BLE001 — provenance is best-effort; never block a render
        return "unknown"


def _stamped_canvas(title: str, revision: str):
    """A canvas that holds each finished page until `save()`, then stamps the footer.

    The total page count is not known while a page is being drawn, so the footer cannot be
    written by a page callback. Deferring every page to save time makes `Page N of M` honest.
    """

    class _Stamped(pdfcanvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pending: list[dict] = []

        def showPage(self):
            self._pending.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pending)
            for number, state in enumerate(self._pending, 1):
                self.__dict__.update(state)
                self.saveState()
                self.setFont("Helvetica", 7)
                self.setFillColor(colors.HexColor("#666666"))
                self.drawString(0.75 * inch, 0.45 * inch, f"{title}  ·  source {revision}")
                self.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Page {number} of {total}")
                self.restoreState()
                super().showPage()
            super().save()

    return _Stamped


_CELL_PADDING = 10  # LEFTPADDING + RIGHTPADDING in _TABLE_STYLE


def _token_cell(text: str, width: float | None = None):
    """A cell of machine identifiers that must never break mid-token. Items separate at commas,
    each on its own line, so a reader copies `platform_account_id` whole.

    RAISES when a token cannot fit the column. `wordWrap=None` with `splitLongWords=0` does not
    wrap an over-wide token — it CLIPS it, silently. That printed `website.review_completed` as
    `website.review_complete`, a value that 422s on arrival and reads as correct on the page. A
    document is worse than useless when it is confidently wrong, so an unfittable token is a build
    failure rather than a layout artefact a reviewer is expected to catch by eye.
    """
    items = [part.strip() for part in str(text).split(",") if part.strip()]
    if width is not None:
        room = width - _CELL_PADDING
        for item in items or [str(text)]:
            needed = stringWidth(item, TOKEN.fontName, TOKEN.fontSize)
            if needed > room:
                raise ValueError(
                    f"{item!r} needs {needed:.1f}pt but its column allows {room:.1f}pt; it would "
                    f"be clipped on the page. Widen the column or reduce the token font."
                )
    return Paragraph("<br/>".join(escape(i) for i in items) or escape(text), TOKEN)


def _prose_cell(text: str, width: float):
    """A wrapping prose cell that never cuts a word in half.

    CELL sets `splitLongWords=0`, so an over-wide word is not split — it is CLIPPED instead, which
    is the silent failure `_token_cell` already refuses. Same rule here: the longest word must fit
    its column, or the build stops.
    """
    room = width - _CELL_PADDING
    for word in str(text).split():
        needed = stringWidth(word, CELL.fontName, CELL.fontSize)
        if needed > room:
            raise ValueError(
                f"{word!r} needs {needed:.1f}pt but its column allows {room:.1f}pt; it would be "
                f"clipped. Widen the column or rephrase around the term."
            )
    return Paragraph(escape(text), CELL)


def escape(text: str) -> str:
    """Registry values are plain text; reportlab's Paragraph parses a mini-HTML, so `>` in a
    direction token like `platform->tool` must be escaped or it is swallowed as markup."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TAG = re.compile(r"<[^>]+>")


def visible_text(markup: str) -> str:
    """What a reader actually sees, given the mini-HTML we hand reportlab.

    The document model records this rather than the markup, because the model exists to be
    compared against the built page — and `<b>` never reaches the page.
    """
    text = str(markup).replace("<br/>", "\n")
    text = _TAG.sub("", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


@dataclass(frozen=True)
class Block:
    """One addressable region of the document, and exactly what it puts on the page.

    `claim_id` is None for structural blocks — headings and connective prose that carry no
    authoritative value. `kind` decides how the block is located on the built page: prose is found
    as a contiguous run of text, a table is read back cell by cell.
    """

    kind: str  # "heading" | "prose" | "code" | "table" | "alert"
    claim_id: str | None
    lines: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = ()  # tables only, header row first
    role: str = "value"  # "value" | "note" — a note is attributed but is not the claim's value
    # The named projection this block was rendered under. The rendering tests recompute the
    # expected content from the REGISTRY through `docs.contracts.projection` using this name, so
    # the renderer never supplies the answer it is checked against (finding 3).
    projection: str = ""
    row_fields: tuple[str, ...] = ()


@dataclass
class Section:
    section_id: str
    title: str
    blocks: list


class Doc:
    """A document under construction, as a typed ordered model of what it will display.

    Re-audit `4f23f23..97deeae` F3. The previous version tracked a LIST OF CLAIM IDS. Counting ids
    and searching the page globally cannot prove what a reader saw: the suite stayed green after
    the visible meaning of a claim was removed while its id stayed recorded, after the eight
    canonical signing lines were reversed on the page, and after a contradictory unclaimed
    paragraph was appended beside the claim it contradicted. All three are invisible to a counter.

    So the document is now a sequence of sections, each a sequence of blocks, each block carrying
    the exact visible lines it emits. `tests/unit/test_document_model.py` compares that model to
    the built PDF span by span, in order, once each, inside the declared section.
    """

    def __init__(self, registry) -> None:
        self.registry = registry
        self.story: list = []
        self.rendered: list[str] = []
        self.sections: list[Section] = []
        self.placements: list[dict] = []  # filled by build(): where each flowable landed
        self._total_pages = 0
        self._open_section("(front matter)", "")

    # ---- the document model ------------------------------------------------------------------
    def _open_section(self, section_id: str, title: str) -> None:
        self.sections.append(Section(section_id=section_id, title=title, blocks=[]))

    def section(self, section_id: str, title: str):
        """Start a numbered section. Its heading is a structural block of the new section."""
        if any(s.section_id == section_id for s in self.sections):
            raise ValueError(f"duplicate section id {section_id!r}")
        self._open_section(section_id, title)
        self.story.append(Paragraph(escape(title), H1))
        self._add(Block(kind="heading", claim_id=None, lines=(title,)))

    def _add(self, block) -> None:
        self.sections[-1].blocks.append(block)

    @property
    def blocks(self) -> list:
        return [block for section in self.sections for block in section.blocks]

    def section_of(self, claim_id: str) -> str | None:
        for section in self.sections:
            if any(b.claim_id == claim_id for b in section.blocks):
                return section.section_id
        return None

    # ---- structural prose (carries no authoritative value) ----------------------------------
    def title(self, text: str):
        self.story.append(Paragraph(escape(text), _styles["Title"]))
        self._add(Block(kind="heading", claim_id=None, lines=(text,)))

    def h1(self, text: str):
        self.story.append(Paragraph(escape(text), H1))
        self._add(Block(kind="heading", claim_id=None, lines=(text,)))

    def h2(self, text: str):
        self.story.append(Paragraph(escape(text), H2))
        self._add(Block(kind="heading", claim_id=None, lines=(text,)))

    def p(self, markup: str, style=BODY):
        """Prose. Bold spans are allowed here, so this takes pre-escaped markup."""
        self.story.append(Paragraph(markup, style))
        self._add(Block(kind="prose", claim_id=None, lines=(visible_text(markup),)))

    def why(self, markup: str):
        self.p(markup, WHY)

    def code(self, text: str):
        """Fixed-width block whose INDENTATION is meaningful — published Python, mainly.

        Uses XPreformatted, not Paragraph: Paragraph collapses leading whitespace, so published
        Python came out unindented and would not compile when copied off the page (re-audit
        `6feca36..4f23f23` F3). The cost is that it never wraps, so `_guard_preformatted` refuses
        a line that would not fit. Use `wrapcode()` for fixed-width text that may wrap.
        """
        self.story.append(XPreformatted(_guard_preformatted(escape(text)), CODE))
        self._add(Block(kind="code", claim_id=None, lines=tuple(text.split("\n"))))

    def wrapcode(self, text: str):
        """Fixed-width text that is allowed to wrap anywhere, including mid-token.

        For the signed body: 163 bytes of JSON with no whitespace, which cannot fit a line and has
        no break opportunity. The document tells the reader it wraps and gives them the byte count
        and sha256 to check their transcription against.
        """
        self.story.append(Paragraph(escape(text), WRAPCODE))
        self._add(Block(kind="code", claim_id=None, lines=(text,)))

    def space(self, height: float = 4):
        self.story.append(Spacer(1, height))

    # ---- claims (the registry owns the value) ------------------------------------------------
    #
    # Recording is ATOMIC with appending a flowable (re-audit `6feca36..4f23f23` F4). There is no
    # public record hook: a caller could otherwise mark a claim rendered and then display nothing,
    # display something else, or display it twice, and the coverage count would still be 1. Every
    # method below appends first and records only on success, so `self.rendered` counts flowables
    # that exist rather than intentions.
    def claim_note(self, claim_id: str, *, style=WHY):
        """Render a claim's NOTE, attributed to that claim.

        Notes were being emitted through `p()`/`why()`, which made them unattributed prose — and
        unattributed prose is exactly what nothing verifies (re-audit `4f23f23..97deeae` F3). The
        receiver contract's note, "a 2xx returned before your commit is unrecoverable", is
        load-bearing guidance sitting outside the claim it belongs to. It does not count as the
        claim's VALUE for coverage purposes, so it carries role="note".
        """
        claim = self.registry[claim_id]
        lines = projection.expected_lines(claim, projection.NOTE)
        self.story.append(Paragraph(escape(lines[0]), style))
        self._add(Block(kind="prose", claim_id=claim_id, lines=lines, role="note",
                        projection=projection.NOTE))

    def _emit(self, claim_id: str, flowables: list, lines, *, kind: str = "prose", rows=(),
              projection_name: str = "", row_fields: tuple[str, ...] = ()):
        """Append the flowables, record the claim, and record EXACTLY what went on the page.

        `lines` is not decoration. Recording an id proves a call happened; recording the visible
        lines is what lets a test find that content on the built page, in order, once, in the
        right section (re-audit `4f23f23..97deeae` F3).
        """
        claim = self.registry[claim_id]
        if not flowables:
            raise ValueError(f"{claim_id} produced no flowable")
        lines = tuple(line for line in lines if str(line).strip())
        if not lines and not rows:
            raise ValueError(f"{claim_id} produced a flowable with no visible text")
        self.story.extend(flowables)
        self.rendered.append(claim_id)
        self._add(Block(kind=kind, claim_id=claim_id, lines=lines, rows=tuple(rows),
                        projection=projection_name, row_fields=row_fields))
        return claim

    def claim_paragraph(self, claim_id: str, *, style=BODY, prefix: str = ""):
        """Render a claim whose value is a single string.

        Refuses a non-string: `escape()` would happily stringify a tuple, and the contract shipped
        `decision is one of: ('approve', 'approve_buy_locked', ...)` — Python repr, quotes and
        parentheses included, in a document written for people implementing against it.
        """
        claim = self.registry[claim_id]
        (line,) = projection.expected_lines(claim, projection.PARAGRAPH)
        markup = prefix + escape(line)
        self._emit(claim_id, [Paragraph(markup, style)], (line,),
                   projection_name=projection.PARAGRAPH)

    def claim_bullets(self, claim_id: str, *, style=BODY):
        """Render a claim whose value is a sequence of strings, one paragraph each."""
        claim = self.registry[claim_id]
        lines = projection.expected_lines(claim, projection.BULLETS)
        self._emit(claim_id, [Paragraph("\u2013  " + escape(line), style) for line in lines],
                   lines, projection_name=projection.BULLETS)

    def _with_heading(self, claim_id: str, heading: str | None, blocks: list, lines, *,
                      kind: str = "prose", projection_name: str = ""):
        if heading:
            self._emit(claim_id, [KeepTogether([Paragraph(escape(heading), H2), *blocks])],
                       (heading, *lines), kind=kind, projection_name=projection_name)
        else:
            self._emit(claim_id, blocks, lines, kind=kind, projection_name=projection_name)

    def claim_steps(self, claim_id: str, *, heading: str | None = None):
        """Render an ORDERED claim as numbered, WRAPPING paragraphs, kept with its heading.

        One paragraph per step, not one preformatted block: steps are sentences, and a preformatted
        block runs the longest one straight off the page (see `_guard_preformatted`).
        """
        claim = self.registry[claim_id]
        lines = projection.expected_lines(claim, projection.STEPS)
        self._with_heading(claim_id, heading,
                           [Paragraph(escape(line).replace(". ", ".  ", 1), STEP) for line in lines],
                           lines, projection_name=projection.STEPS)

    def claim_code(self, claim_id: str, *, heading: str | None = None, numbered: bool = False,
                   lead: str | None = None):
        """Render a claim whose value is a sequence of literals as a fixed-width block.

        `numbered` uses the NUMBERED_CODE projection, so the numbering comes from the registry
        rather than from an f-string in the caller (finding 3): the canonical signing block was
        numbered by the generator, which meant the generator authored the very lines the page was
        checked against.
        """
        claim = self.registry[claim_id]
        name = projection.NUMBERED_CODE if numbered else projection.CODE
        lines = projection.expected_lines(claim, name)
        block = XPreformatted(_guard_preformatted("\n".join(escape(v) for v in lines)), CODE)
        flowables = [Paragraph(lead, BODY), block] if lead else [block]
        self._with_heading(claim_id, heading, flowables,
                           ((visible_text(lead),) + lines) if lead else lines,
                           kind="code", projection_name=name)

    def claim_prose(self, claim_id: str, markup: str, *, style=BODY):
        """Render a claim as prose the caller composed FROM that claim's value.

        Still atomic: the flowable and the record are appended together. The rendering tests are
        the other half — they assert the claim's own value reaches the extracted page text, so
        composing a paragraph that omits or contradicts the value fails there.
        """
        self._emit(claim_id, [Paragraph(markup, style)], (visible_text(markup),),
                   projection_name=projection.COMPOSED)

    _PART_STYLES = {"p": BODY, "why": WHY}

    def claim_mixed(self, claim_id: str, parts, *, published_fields: tuple[str, ...] = ()):
        """Render one claim that needs several flowables — prose, then a code block, then more.

        `parts` is a sequence of (kind, text) pairs where kind is "p", "why", "code" (fixed-width,
        indentation preserved, must fit the line), "atomic_code" (the same, but never split across
        a page), or "wrap" (fixed-width, wraps anywhere). Prose parts take pre-escaped markup;
        the code and wrap kinds take raw text and are escaped here.

        This exists so a composite claim — the signing vector is a body, a canonical string, a
        digest, and a runnable snippet — stays ONE atomic emit. The alternative was a public
        record hook beside a pile of loose `p()`/`code()` calls, which is exactly the shape that
        lets a claim count as covered while displaying something else (re-audit F4).
        """
        flowables = []
        for kind, text in parts:
            if kind == "code":
                flowables.append(XPreformatted(_guard_preformatted(escape(text)), CODE))
            elif kind == "atomic_code":
                # ONE page, always. A copyable block split across pages has the page footer
                # physically between two statements, so a contiguous copy picks up
                # "... Page 5 of 7" and fails to compile (re-audit `4f23f23..97deeae` finding 4).
                flowables.append(
                    KeepTogether([XPreformatted(_guard_preformatted(escape(text)), CODE)]))
            elif kind == "wrap":
                flowables.append(Paragraph(escape(text), WRAPCODE))
            else:
                flowables.append(Paragraph(text, self._PART_STYLES[kind]))
        lines = []
        for kind, text in parts:
            lines.extend(
                (text if kind in ("code", "atomic_code", "wrap") else visible_text(text)).split("\n"))
        self._emit(claim_id, flowables, tuple(lines), projection_name=projection.COMPOSED,
                   row_fields=published_fields)

    def claim_alert(self, claim_id: str):
        """A blocked claim, rendered where a reader would otherwise act on the document."""
        claim = self.registry[claim_id]
        lines = projection.expected_lines(claim, projection.ALERT)
        body = "<br/>".join("\u2013  " + escape(line) for line in lines)
        self._emit(claim_id, [Paragraph(body, ALERT)], lines, kind="alert",
                   projection_name=projection.ALERT)

    def claim_table(
        self,
        claim_id: str,
        headers: tuple[str, ...],
        widths,
        rows=None,
        heading: str | None = None,
        code_columns: tuple[int, ...] = (),
        row_fields: tuple[str, ...] = (),
    ):
        """Render a claim whose value is a sequence of row tuples.

        `code_columns` marks columns holding machine identifiers. Those render in a fixed-width
        face and break only between comma-separated items, never inside a token: an identifier
        split across lines as `platform` / `_account_id` is one a reader copies wrong, and payload
        extras are accepted, so the misspelling 202s while silently populating nothing
        (re-audit `6feca36..4f23f23` F10).
        """
        claim = self.registry[claim_id]
        # TABLE projects the claim's own rows; COMPOSED means the caller assembled them and the
        # test instead requires every leaf of the claim's value to appear somewhere in the table.
        name = projection.COMPOSED if rows is not None else projection.TABLE
        if name == projection.TABLE:
            rows = projection.expected_rows(claim, projection.TABLE, row_fields)
        data = [[Paragraph(escape(h), CELLB) for h in headers]]
        for row in rows:
            data.append(
                [
                    _token_cell(cell, widths[index])
                    if index in code_columns
                    else _prose_cell(cell, widths[index])
                    for index, cell in enumerate(row)
                ]
            )
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(_TABLE_STYLE)
        flowables = [table]
        lines = (heading,) if heading else ()
        if heading:
            flowables = [KeepTogether([Paragraph(escape(heading), H2), table])]
        body_rows = tuple(tuple(str(cell) for cell in row) for row in rows)
        self._emit(claim_id, flowables, lines, kind="table",
                   rows=((tuple(headers),) + body_rows),
                   projection_name=name, row_fields=row_fields)

    def table(self, headers: tuple[str, ...], rows, widths, code_columns: tuple[int, ...] = ()):
        """A table whose cells are already formatted from claims recorded elsewhere."""
        data = [[Paragraph(escape(h), CELLB) for h in headers]]
        for row in rows:
            data.append(
                [
                    _token_cell(cell, widths[index])
                    if index in code_columns
                    else _prose_cell(cell, widths[index])
                    for index, cell in enumerate(row)
                ]
            )
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(_TABLE_STYLE)
        self.story.append(table)
        self._add(Block(kind="table", claim_id=None, lines=(),
                        rows=(tuple(headers),) + tuple(tuple(str(c) for c in r) for r in rows)))

    def build(self, path: str, title: str, *, release: bool = False) -> str:
        """Render to `path`, stamping provenance on every page.

        A document read for years without the repo beside it needs to say which commit produced
        it and how many pages it has, so a stale copy is distinguishable from the audited one and
        a missing page is visible (re-audit `6feca36..4f23f23` F12, document half).

        "Page N of M" needs the total, which is only known once the last page is laid out. This
        makes ONE layout pass and defers the furniture to canvas save time. A counting pass over
        the same story does not work: reportlab mutates flowables as it lays them out (frame
        binding, split state), so a second build over already-rendered objects raises LayoutError.
        """
        revision = source_revision()
        self.placements = []
        if release:
            # A preview may be built from anything. A RELEASE artifact is the thing someone will
            # still be holding in a year, so it must name a commit anyone can check out.
            if revision == "unknown":
                raise ProvenanceError(
                    "release build cannot determine the source commit; refusing to stamp a PDF "
                    "'source unknown'. Build from a git checkout."
                )
            if revision.endswith("+dirty"):
                raise ProvenanceError(
                    f"release build has uncommitted changes ({revision}); the stamped commit "
                    "would not reproduce these pages. Commit first, then rebuild."
                )
        placements = self.placements

        class _Recording(SimpleDocTemplate):
            """Records where each flowable actually landed.

            Geometry read back from the PAGE cannot see two flowables drawn on the same baseline:
            the extractor merges their glyphs into a single word, so a line-box detector reports
            zero collisions on a visibly interleaved page (re-audit `4f23f23..122cc67` finding 11).
            The layout engine is the only place those rectangles exist.
            """

            def afterFlowable(self, flowable):  # noqa: N802 — reportlab's spelling
                frame = getattr(self, "frame", None)
                if frame is None:
                    return
                height = getattr(flowable, "height", 0) or 0
                width = getattr(flowable, "width", 0) or 0
                placements.append({
                    "page": self.page,
                    "x0": frame._x1,
                    "x1": frame._x1 + width,
                    "bottom": frame._y,
                    "top": frame._y + height,
                    "what": type(flowable).__name__,
                    "text": " ".join(str(getattr(flowable, "text", ""))[:40].split()),
                })

        template = _Recording(
            path,
            pagesize=letter,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            topMargin=0.7 * inch,
            bottomMargin=0.8 * inch,
            title=title,
            author="IPv4.Global",
            subject=f"source revision {revision}",
        )
        template.build(self.story, canvasmaker=_stamped_canvas(title, revision))
        self._total_pages = template.page
        return path


INCH = inch
