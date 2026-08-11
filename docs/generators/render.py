"""Shared reportlab scaffolding for the TechCraft documents.

The renderers pull every authoritative value from `docs.contracts`. Prose here explains; it never
restates a value the registry owns. `Doc.claim()` records which claim ids reached a flowable, so
`tests/unit/test_contract_rendering.py` can prove coverage structurally as well as by extracting
text from the built PDF.
"""

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)

_styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1x", parent=_styles["Heading1"], fontSize=15, spaceBefore=16, spaceAfter=6,
                    textColor=colors.HexColor("#1a1a2e"))
H2 = ParagraphStyle("H2x", parent=_styles["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4,
                    textColor=colors.HexColor("#1a1a2e"))
BODY = ParagraphStyle("Bodyx", parent=_styles["Normal"], fontSize=9.5, leading=13, spaceAfter=5)
WHY = ParagraphStyle("Why", parent=BODY, leftIndent=10, textColor=colors.HexColor("#444444"),
                     fontSize=9, leading=12)
CODE = ParagraphStyle("Code", parent=_styles["Code"], fontSize=8, leading=10.5, leftIndent=8,
                      spaceAfter=5, backColor=colors.HexColor("#f4f4f4"))
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.5, leading=11, spaceAfter=0)
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


def escape(text: str) -> str:
    """Registry values are plain text; reportlab's Paragraph parses a mini-HTML, so `>` in a
    direction token like `platform->tool` must be escaped or it is swallowed as markup."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Doc:
    """A document under construction, tracking which registry claims it rendered."""

    def __init__(self, registry) -> None:
        self.registry = registry
        self.story: list = []
        self.rendered: list[str] = []

    # ---- prose (carries no authoritative value) --------------------------------------------
    def title(self, text: str):
        self.story.append(Paragraph(escape(text), _styles["Title"]))

    def h1(self, text: str):
        self.story.append(Paragraph(escape(text), H1))

    def h2(self, text: str):
        self.story.append(Paragraph(escape(text), H2))

    def p(self, markup: str, style=BODY):
        """Prose. Bold spans are allowed here, so this takes pre-escaped markup."""
        self.story.append(Paragraph(markup, style))

    def why(self, markup: str):
        self.p(markup, WHY)

    def code(self, text: str):
        """Fixed-width block. Uses XPreformatted, not Paragraph: Paragraph collapses leading
        whitespace, so published Python came out unindented and would not compile when copied off
        the page (re-audit `6feca36..4f23f23` F3)."""
        self.story.append(XPreformatted(escape(text), CODE))

    def space(self, height: float = 4):
        self.story.append(Spacer(1, height))

    # ---- claims (the registry owns the value) ------------------------------------------------
    def _record(self, claim_id: str):
        self.rendered.append(claim_id)
        return self.registry[claim_id]

    def claim_paragraph(self, claim_id: str, *, style=BODY, prefix: str = ""):
        """Render a claim whose value is a single string."""
        claim = self._record(claim_id)
        self.story.append(Paragraph(prefix + escape(claim.value), style))

    def claim_bullets(self, claim_id: str, *, style=BODY):
        """Render a claim whose value is a sequence of strings, one paragraph each."""
        claim = self._record(claim_id)
        for item in claim.value:
            self.story.append(Paragraph("\u2013  " + escape(item), style))

    def claim_steps(self, claim_id: str):
        """Render an ORDERED claim as a numbered block, so the reader sees the order the test
        asserts."""
        claim = self._record(claim_id)
        lines = [f"{n}. {escape(step)}" for n, step in enumerate(claim.value, 1)]
        self.story.append(Paragraph("<br/>".join(lines), CODE))

    def claim_alert(self, claim_id: str):
        """A blocked claim, rendered where a reader would otherwise act on the document."""
        claim = self._record(claim_id)
        body = "<br/>".join("\u2013  " + escape(item) for item in claim.value)
        self.story.append(Paragraph(body, ALERT))

    def claim_table(self, claim_id: str, headers: tuple[str, ...], widths, rows=None):
        """Render a claim whose value is a sequence of row tuples."""
        claim = self._record(claim_id)
        data = [[Paragraph(escape(h), CELLB) for h in headers]]
        for row in (rows if rows is not None else claim.value):
            data.append([Paragraph(escape(cell), CELL) for cell in row])
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(_TABLE_STYLE)
        self.story.append(table)

    def table(self, headers: tuple[str, ...], rows, widths):
        """A table whose cells are already formatted from claims recorded elsewhere."""
        data = [[Paragraph(escape(h), CELLB) for h in headers]]
        for row in rows:
            data.append([Paragraph(escape(cell), CELL) for cell in row])
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(_TABLE_STYLE)
        self.story.append(table)

    def build(self, path: str, title: str) -> str:
        SimpleDocTemplate(
            path, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
            topMargin=0.7 * inch, bottomMargin=0.7 * inch, title=title,
        ).build(self.story)
        return path


INCH = inch
