"""Canonical reader for the ROADMAP §C migration-reservation table.

§C is the one place that binds a migration number to the unit that owns it and to its
state (`shipped`/`pending`). Two guards need those numbers: the lineage guard
(`tests/unit/test_migration_lineage.py`) pins §C against the real Alembic chain, and the
artifact gate (`tests/unit/test_plan_artifact_static.py`) bans revision numbers that a
release made stale.

Each used to carry its own copy of the numbers, and the artifact gate's copy had to be
hand-patched on every release — which is how the gate written to catch stale migration
numbers ended up one revision stale itself: `023` shipped, its ban list still stopped at
`022`, so the number activation had just vacated was permitted and the failure message
named the wrong revisions. Reading the numbers from here means a release updates §C and
the guards follow; the lineage guard is what keeps §C itself honest against Alembic.

Everything is read off disk — no database.
"""

import hashlib
import html
import re
from dataclasses import dataclass
from typing import NamedTuple

from kyc_tool.config import REPO_ROOT

ROADMAP = REPO_ROOT / ".agents" / "ROADMAP.md"

# scripts/package_handoff.sh ships the product tree without the internal working files this
# module parses — and without `.git`, which every checkout has. Suites that govern those
# internal files key their skip on THIS flag, never on a governed file's own existence:
# deleting ROADMAP.md in a checkout must FAIL its governance tests, not skip them.
SOURCE_HANDOFF = not (REPO_ROOT / ".git").exists()


class CRow(NamedTuple):
    """One §C data row with EVERY cell retained (re-audit `1826661..b5c7a83` finding 9 — the
    old tuple discarded the Content cell, so a rewritten reservation meaning parsed as
    unchanged). The first three fields keep the historical tuple order, so positional
    consumers of (unit, state, revisions) still read the same values."""

    unit: str
    state: str
    revisions: tuple[int, ...]
    items: str
    migration: str
    content: str


# Backwards-compatible alias: one per §C data row, in table order.
Record = CRow

_SECTION_C = "## C."


def section_c(text: str) -> str:
    """EXACTLY the §C section: from the `## C.` heading to the next `## ` heading.

    Reservations are read from here and nowhere else. The whole-document scan this replaces
    accepted a `| PR … |` row ANYWHERE — so a reservation row relocated into an appendix still
    parsed as authority, and a §C-shaped row in prose could shadow the table
    (Codex re-audit `45cc215` F7). `reservation_rows_outside_section_c` is the other half:
    a stray row is an ERROR, not merely invisible.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(_SECTION_C))
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def parse_records(text: str) -> list[Record]:
    """Ordered typed rows (EVERY cell retained) from a §C-shaped table, NO deduplication —
    a revision double-booked across two rows must survive as two records so the duplicate
    is visible. Columns: `| Unit | Item(s) | State | Migration | Content |` — EXACTLY five;
    a `| PR …` row with any other shape raises rather than being silently skipped, because
    a malformed row is a reservation the parser would otherwise orphan."""
    records: list[Record] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("| PR "):  # data rows only (skips header/separator)
            continue
        # maxsplit=4: the Content cell legitimately contains literal `|` (e.g. `legacy|attempt_v1`),
        # so everything past the fourth delimiter is one cell — a naive split would mis-shape it.
        cells = [c.strip() for c in line.strip().strip("|").split("|", 4)]
        if len(cells) != 5:
            raise ValueError(
                f"§C row has {len(cells)} cells, want exactly 5 "
                f"(Unit | Item(s) | State | Migration | Content): {line.strip()!r}"
            )
        unit, items, state, migration, content = cells
        revisions = tuple(int(tok) for tok in re.findall(r"\b0\d\d\b", migration))
        records.append(CRow(unit=unit, state=state, revisions=revisions,
                            items=items, migration=migration, content=content))
    return records


def records() -> list[Record]:
    """The live §C table — and ONLY §C."""
    return parse_records(section_c(ROADMAP.read_text()))


def reservation_rows_outside_section_c(text: str) -> list[str]:
    """Reservation-shaped `| PR … |` rows carrying a revision number OUTSIDE §C's LINE SPAN.

    To a reader such a row looks like a reservation; to the (§C-anchored) parser it does not
    exist. That divergence is exactly how a reservation could be 'relocated' out of the
    authority table while still reading as reserved. Located by LINE SPAN, not row-text
    membership: the first version of this check used a set of §C row strings, so an EXACT
    duplicate of a §C row pasted elsewhere was invisible — two apparent authorities, one
    checked (re-audit `f495de8` F8)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(_SECTION_C))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    # R-audit-3 finding 13: EVERY PR-shaped row outside §C is refused — including a
    # no-migration `future` reservation. R-audit-4 finding 7: "PR-shaped" is what a READER
    # sees, so the match is structural — compact GFM rows (`|PR 5d|...`), blockquoted rows,
    # and entity-encoded cells (`P&#82;`) are all rows; the scan runs on entity-decoded text
    # and tolerates missing cell padding and quote markers.
    pr_row = re.compile(r"^\s*(?:>\s*)*\|\s*PR\b")

    def rendered(line: str) -> str:
        # R-audit-5 finding 3: `P<!-- hidden -->R 5d` renders as PR 5d — comments vanish for
        # the reader, so they vanish before the match; an unclosed comment hides to EOL.
        line = re.sub(r"<!--.*?-->", "", line)
        line = re.sub(r"<!--.*$", "", line)
        # R-audit-6 finding 3: inline HTML tags do not display — `P<span></span>R` reads as
        # PR — so they vanish before the match too. R-audit-7 finding 4: quote-AWARE, so a
        # `>` inside a quoted attribute cannot terminate the tag early.
        line = re.sub(r"</?[A-Za-z!](?:\"[^\"]*\"|'[^']*'|[^>])*>", "", line)
        return html.unescape(line)

    # R-audit-8 finding 3 (widening R-audit-7 finding 4): Markdown emphasis, links, code
    # spans, and entities hide `PR` from any row regex while a reader still sees a
    # reservation — the same source-vs-rendered split, one syntax over. §C is the ONLY
    # table this authority publishes, so the closed boundary is shape, not content: EVERY
    # table-shaped row outside §C's line span refuses OUTRIGHT, before its cells are
    # interpreted at all. The rendered() strip still catches rows whose leading `|` is
    # itself manufactured by markup (an HTML wrapper ahead of the pipe).
    table_shaped = re.compile(r"^\s*(?:>\s*)*\|")
    # R-audit-9 finding 3: a raw HTML table renders as a table too — the same visible
    # competing authority with no pipe in sight — so HTML table constructs outside §C
    # refuse outright as well. Entity-encoded lookalikes (`&lt;table&gt;`) decode to
    # literal TEXT, never markup, and are rightly ignored here.
    html_table = re.compile(r"</?(?:table|thead|tbody|tfoot|tr|td|th)\b", re.IGNORECASE)
    return [
        line.strip()
        for i, line in enumerate(lines)
        if not (start < i < end) and (
            table_shaped.match(line)
            or html_table.search(line)
            or pr_row.match(rendered(line))
        )
    ]


def shipped(recs: list[Record]) -> list[int]:
    """Every revision owned by a `shipped` row, sorted."""
    return sorted(rev for r in recs if r.state == "shipped" for rev in r.revisions)


def pending(recs: list[Record]) -> list[int]:
    """Every revision owned by a `pending` row, sorted."""
    return sorted(rev for r in recs if r.state == "pending" for rev in r.revisions)


def unit_revisions(recs: list[Record], unit: str) -> list[int]:
    """Every revision reserved by unit `unit` and its sub-rows, sorted.

    One unit spans many §C rows — `PR 7b-core`, `PR 7b-core repair`, … `PR 7b-core
    cross-table authority repair` — and the whole family is what a caller means by
    "7b-core's revisions". Matching is `unit` exactly or `unit` followed by a space, not a
    bare prefix: `startswith("PR 1")` would swallow `PR 10`.
    """
    return sorted(
        rev
        for r in recs
        if r.unit == unit or r.unit.startswith(unit + " ")
        for rev in r.revisions
    )


def head(recs: list[Record]) -> int:
    """The highest shipped reservation — equal to the live Alembic head, which
    `test_roadmap_lineage_consistent_with_alembic` is what actually proves."""
    revs = shipped(recs)
    assert revs, "ROADMAP §C reserves no shipped migration"
    return revs[-1]


# ── future units: lifecycle bound by type, not inferred from a dash ───────────────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 12. A `—` State means only "no migration" — shipped
# code-only rows carry it too — so nothing distinguished a reserved unbuilt unit from a shipped
# one, and deleting the §G scope sections or relabelling the §C rows as shipped left every
# verifier green. Future units now carry the explicit `future` State (legal ONLY without a
# migration; the lineage guard enforces that side), and this registry binds each one's §C row
# AND its §G scope section by digest: deletion, promotion, and a rewritten scope all become
# located failures, and a scope re-pin is the reviewed act.

@dataclass(frozen=True)
class FutureUnit:
    """One reserved unbuilt unit: the EXACT reviewed §C row (every cell typed — re-audit
    `1826661..b5c7a83` finding 9 replaced a state-only bind that let the Content cell say
    anything) plus its §G scope section pinned by digest. Editing either is a re-pin here, the
    act of review."""

    row: CRow
    scope_heading: str
    scope_digest: str  # sha256[:16] of the exact section text, heading line included


FUTURE_UNITS = {
    "PR 5c": FutureUnit(
        row=CRow(
            unit="PR 5c", state="future", revisions=(), items="—", migration="—",
            content="per-key HMAC retirement evidence (reserved by audit fold "
                    "`4c3015a..cccd5f7` F3, NOT built): durable fleet-wide per-key acceptance "
                    "witness with a defined zero window, or signed signer-fleet cutover "
                    "receipt with bounded observation, plus an HMAC signer-target "
                    "drained-cutover record (target key id, secret digest, exact attested "
                    "publisher roles). Unblocks the two retirement steps WIRE.SIGN.ROTATION "
                    "publishes as BLOCKED; shipping it must register the authority in "
                    "kyc_tool.capabilities (the runtime registry every retirement consumer "
                    "resolves), flip the absence anchors, and rewrite the gates in "
                    "docs/contracts/wire.py in the same change"),
        scope_heading="### PR 5c — Per-key HMAC retirement evidence — FUTURE, reserved "
                      "unbuilt (audit fold `4c3015a..cccd5f7` F3)",
        scope_digest="e8e6dd3102162c39"),
    "PR 7b-inputs": FutureUnit(
        row=CRow(
            unit="PR 7b-inputs", state="future", revisions=(), items="—", migration="—",
            content="platform answer artifacts (reserved by audit fold `4c3015a..cccd5f7` "
                    "F11, NOT built): versioned, approved/signed answer-artifact schema and "
                    "its verifying authority for the O1-O4 obligations behind "
                    "WIRE.ORDERING.PENDING_INPUTS. Until it ships, `resolution_problems` in "
                    "docs/contracts/wire.py refuses every artifact — the acceptors are "
                    "content screens, and screening is not resolution. Shipping it must "
                    "register the verifying authority in kyc_tool.capabilities, replace "
                    "ANSWER_ARTIFACT_SCHEMA, and rewrite the gate in the same change"),
        scope_heading="### PR 7b-inputs — Platform answer artifacts — FUTURE, reserved "
                      "unbuilt (audit fold `4c3015a..cccd5f7` F11)",
        scope_digest="cfebec33c6e40758"),
}


def _scope_section(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line == heading]
    if len(starts) != 1:
        return None
    end = next((i for i in range(starts[0] + 1, len(lines))
                if lines[i].startswith("### ")), len(lines))
    return "\n".join(lines[starts[0]:end])


def future_unit_problems(text: str) -> list[str]:
    """Why the document no longer honors its future-unit reservations; empty when it does.

    Three binds (re-audit `1826661..b5c7a83` finding 9): duplicate unit names are refused
    BEFORE any row is selected; every registered unit's live row must EQUAL the reviewed row —
    every cell, Content included; and the set of `future` rows must equal the registry
    exactly, so an unregistered reservation is an error, not non-input."""
    problems = []
    try:
        recs = parse_records(section_c(text))
    except (StopIteration, ValueError) as exc:
        return [f"§C is unreadable: {exc}"]
    # R-audit-5 finding 3: a CLOSED comment hides characters from the scan; an UNCLOSED one
    # hides the rest of the rendered document. Neither may exist in the reservation authority.
    unclosed = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if "<!--" in unclosed:
        problems.append("an unclosed HTML comment hides the rest of the rendered document "
                        "from every reader and scanner; refuse the document")
    for stray in reservation_rows_outside_section_c(text):
        problems.append(f"a table-shaped row outside §C can read as a reservation while "
                        f"invisible to this authority; refused outright: {stray[:70]!r}")
    if problems:
        return problems
    names = [r.unit for r in recs]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{name}: duplicate §C unit rows — two apparent reservations, at "
                        "most one checked")
    if problems:
        return problems
    future_rows = {r.unit for r in recs if r.state == "future"}
    for extra in sorted(future_rows - set(FUTURE_UNITS)):
        problems.append(
            f"{extra}: a `future` reservation row the registry never reviewed — register it "
            "with its exact cells and scope digest, or it does not exist")
    for unit, spec in FUTURE_UNITS.items():
        row = next((r for r in recs if r.unit == unit), None)
        if row is None:
            problems.append(f"{unit}: the §C reservation row is gone")
        elif row != spec.row:
            diverged = [f for f in CRow._fields if getattr(row, f) != getattr(spec.row, f)]
            problems.append(
                f"{unit}: the §C row diverges from the reviewed cells ({', '.join(diverged)})"
                " — read the new row, then re-pin the registry in the same commit")
        section = _scope_section(text, spec.scope_heading)
        if section is None:
            problems.append(f"{unit}: the §G scope section is missing (or duplicated)")
            continue
        digest = hashlib.sha256(section.encode()).hexdigest()[:16]
        if digest != spec.scope_digest:
            problems.append(
                f"{unit}: the §G scope changed since it was reviewed (was "
                f"{spec.scope_digest}, now {digest}) — read the new scope, then re-pin in "
                "the same commit"
            )
    return problems
