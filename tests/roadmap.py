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

import re

from kyc_tool.config import REPO_ROOT

ROADMAP = REPO_ROOT / ".agents" / "ROADMAP.md"

# `(unit, state, revisions)` — one per §C data row, in table order.
Record = tuple[str, str, list[int]]

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
    """Ordered `(unit, state, [revisions])` from a §C-shaped table, NO deduplication —
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
        unit, state, migration = cells[0], cells[2], cells[3]
        revisions = [int(tok) for tok in re.findall(r"\b0\d\d\b", migration)]
        records.append((unit, state, revisions))
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
    return [
        line.strip()
        for i, line in enumerate(lines)
        if not (start < i < end)
        and line.lstrip().startswith("| PR ")
        and re.search(r"\b0\d\d\b", line)
    ]


def shipped(recs: list[Record]) -> list[int]:
    """Every revision owned by a `shipped` row, sorted."""
    return sorted(rev for _unit, state, revs in recs if state == "shipped" for rev in revs)


def pending(recs: list[Record]) -> list[int]:
    """Every revision owned by a `pending` row, sorted."""
    return sorted(rev for _unit, state, revs in recs if state == "pending" for rev in revs)


def unit_revisions(recs: list[Record], unit: str) -> list[int]:
    """Every revision reserved by unit `unit` and its sub-rows, sorted.

    One unit spans many §C rows — `PR 7b-core`, `PR 7b-core repair`, … `PR 7b-core
    cross-table authority repair` — and the whole family is what a caller means by
    "7b-core's revisions". Matching is `unit` exactly or `unit` followed by a space, not a
    bare prefix: `startswith("PR 1")` would swallow `PR 10`.
    """
    return sorted(
        rev
        for name, _state, revs in recs
        if name == unit or name.startswith(unit + " ")
        for rev in revs
    )


def head(recs: list[Record]) -> int:
    """The highest shipped reservation — equal to the live Alembic head, which
    `test_roadmap_lineage_consistent_with_alembic` is what actually proves."""
    revs = shipped(recs)
    assert revs, "ROADMAP §C reserves no shipped migration"
    return revs[-1]
