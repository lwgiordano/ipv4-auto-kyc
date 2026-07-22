"""Migration-lineage guard (audit rounds 2 + 3).

Round 1's P1 fix authored validation revision 012, which consumed the migration
number the ROADMAP §C still reserved for the pending PR 6b — a real forward
collision (a future migration PR built from the canonical plan would duplicate `012`
or re-branch from `011`, yielding a multiple-head Alembic graph that breaks the
migration CI gate and `/readyz`'s single-head lookup).

Round 3 showed the first guard was too weak: it reduced the table to a `set[int]` and
checked only numeric coverage, so it recorded neither the owning unit nor its
shipped/pending state — the exact ownership collision (an authored revision still
"reserved" for a pending unit) and a duplicate reservation both slipped through. This
guard parses the §C table into ordered `(unit, state, revisions)` records **without
deduplication** and validates ownership against the live Alembic chain. Everything is
read off disk — no database.
"""

import re

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from kyc_tool.config import REPO_ROOT


def _script() -> ScriptDirectory:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _parse_roadmap_records(text: str) -> list[tuple[str, str, list[int]]]:
    """Ordered `(unit, state, [revisions])` from the §C table, NO deduplication —
    a revision double-booked across two rows must survive as two records so the
    duplicate is visible. Columns: `| Unit | Item(s) | State | Migration | Content |`."""
    records: list[tuple[str, str, list[int]]] = []
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


def _validate_lineage(
    records: list[tuple[str, str, list[int]]], existing: set[int], head_num: int
) -> None:
    """Pure validator (raises AssertionError on any violation) so it can be exercised
    with crafted records, not only the live ROADMAP."""
    flat = [rev for _unit, _state, revs in records for rev in revs]
    dupes = sorted({rev for rev in flat if flat.count(rev) > 1})
    assert not dupes, f"duplicate migration reservations in ROADMAP §C: {dupes}"

    reserved = sorted(flat)  # unique now (dup check passed)
    assert reserved, "ROADMAP §C reserves no migrations"
    assert reserved == list(range(reserved[0], reserved[-1] + 1)), (
        f"ROADMAP migration reservations are not contiguous: {reserved}"
    )

    shipped = {rev for _unit, state, revs in records if state == "shipped" for rev in revs}
    pending = sorted(rev for _unit, state, revs in records if state == "pending" for rev in revs)

    # every authored revision at/above the first reservation must be owned by a shipped
    # row, and every shipped revision must be authored — equality catches an authored
    # revision still "reserved" for a pending unit (the round-3 ownership collision).
    authored_reserved = {rev for rev in existing if rev >= reserved[0]}
    assert authored_reserved == shipped, (
        f"authored reserved revisions {sorted(authored_reserved)} != shipped-owned "
        f"{sorted(shipped)}; a migration is authored but still marked pending (or vice versa)"
    )
    # no pending revision may already exist in the Alembic chain
    collided = sorted(set(pending) & existing)
    assert not collided, f"pending ROADMAP revisions already authored: {collided}"
    # the live head is shipped; the next migration to author is the first pending == head+1
    assert head_num in shipped, f"live Alembic head {head_num:03d} is not a shipped reservation"
    assert pending, "ROADMAP §C reserves no pending migration"
    assert pending[0] == head_num + 1, (
        f"first pending migration {pending[0]:03d} != head+1 ({head_num + 1:03d}); "
        "renumber the ROADMAP reservations after adding a migration"
    )


def test_single_alembic_head():
    heads = _script().get_heads()
    assert len(heads) == 1, f"expected exactly one Alembic head, got {heads}"


def test_roadmap_lineage_consistent_with_alembic():
    script = _script()
    existing = {int(r.revision) for r in script.walk_revisions()}
    head_num = int(script.get_heads()[0])
    records = _parse_roadmap_records((REPO_ROOT / ".agents" / "ROADMAP.md").read_text())
    _validate_lineage(records, existing, head_num)


# --- mutation resistance: the validator must reject the exact collisions round 3 named ---


def _shipped_through_012() -> list[tuple[str, str, list[int]]]:
    return [
        ("PR 2", "shipped", [8]),
        ("PR 4", "shipped", [9]),
        ("PR 5a", "shipped", [10]),
        ("PR 6", "shipped", [11, 12]),
    ]


def test_validator_rejects_ownership_collision():
    # the pre-fix shape: PR 6 (shipped) owns only 011; the PENDING PR 6b still reserves
    # 012 — which is already authored. Numerically gap-free, but the ownership is wrong.
    records = [
        ("PR 2", "shipped", [8]),
        ("PR 4", "shipped", [9]),
        ("PR 5a", "shipped", [10]),
        ("PR 6", "shipped", [11]),
        ("PR 6b", "pending", [12]),
        ("PR 7a", "pending", [13]),
        ("PR 7b", "pending", [14]),
        ("PR 8", "pending", [15]),
        ("PR 10", "pending", [16]),
    ]
    with pytest.raises(AssertionError):
        _validate_lineage(records, existing=set(range(1, 13)), head_num=12)


def test_validator_rejects_duplicate_reservation():
    # two pending rows book 013; the numeric RANGE stays gap-free (013..017 present),
    # so only the pre-dedup duplicate check can catch it.
    records = _shipped_through_012() + [
        ("PR 6b", "pending", [13]),
        ("PR 7a", "pending", [13]),
        ("PR 7b", "pending", [14]),
        ("PR 8", "pending", [15]),
        ("PR 10", "pending", [16, 17]),
    ]
    with pytest.raises(AssertionError):
        _validate_lineage(records, existing=set(range(1, 13)), head_num=12)


def test_validator_accepts_the_real_shape():
    # sanity: the corrected shape (head 012 shipped, next pending 013) passes.
    records = _shipped_through_012() + [
        ("PR 6b", "pending", [13]),
        ("PR 7a", "pending", [14]),
        ("PR 7b", "pending", [15]),
        ("PR 8", "pending", [16]),
        ("PR 10", "pending", [17]),
    ]
    _validate_lineage(records, existing=set(range(1, 13)), head_num=12)
