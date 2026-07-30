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


def parse_records(text: str) -> list[Record]:
    """Ordered `(unit, state, [revisions])` from the §C table, NO deduplication —
    a revision double-booked across two rows must survive as two records so the
    duplicate is visible. Columns: `| Unit | Item(s) | State | Migration | Content |`."""
    records: list[Record] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("| PR "):  # data rows only (skips header/separator)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        unit, state, migration = cells[0], cells[2], cells[3]
        revisions = [int(tok) for tok in re.findall(r"\b0\d\d\b", migration)]
        records.append((unit, state, revisions))
    return records


def records() -> list[Record]:
    """The live §C table."""
    return parse_records(ROADMAP.read_text())


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
