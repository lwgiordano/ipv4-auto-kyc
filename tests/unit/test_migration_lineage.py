"""Migration-lineage guard (audit rounds 2 + 3 + 4).

Round 1's P1 fix authored validation revision 012, which consumed the migration
number the ROADMAP §C still reserved for the pending PR 6b — a real forward collision
(a multiple-head Alembic graph breaks the migration CI gate and `/readyz`'s single-head
lookup). Round 2 added a numeric guard; round 3 showed it ignored ownership; round 4
showed it silently ignored an *unknown* State (a typo like `pendng`) — orphaning a
revision from both ownership sets — and that the negative tests bypassed the parser.

This guard parses the §C table into ordered `(unit, state, revisions)` records **without
deduplication** and fail-closes on any State that is not exactly `shipped`/`pending`/`—`,
so `shipped ∪ pending` is a disjoint, exhaustive partition of the reserved revisions.
The regression tests drive crafted Markdown through the real parser, not hand-built
records, so a parser regression (reintroduced dedup, a shifted column) is caught too.
Everything is read off disk — no database.
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
    with crafted tables, not only the live ROADMAP."""
    # State contract, fail-closed: a row with a migration is shipped|pending; a row
    # without one is '—'. An unrecognised State (e.g. the typo 'pendng') must NOT be
    # silently dropped — that would orphan its revision from both ownership sets.
    for unit, state, revs in records:
        if revs:
            assert state in ("shipped", "pending"), (
                f"{unit}: migration row has unrecognised State {state!r} (want shipped|pending)"
            )
        else:
            assert state == "—", f"{unit}: non-migration row has State {state!r} (want '—')"

    flat = [rev for _unit, _state, revs in records for rev in revs]
    dupes = sorted({rev for rev in flat if flat.count(rev) > 1})
    assert not dupes, f"duplicate migration reservations in ROADMAP §C: {dupes}"

    reserved = sorted(flat)  # unique now (dup check passed)
    assert reserved, "ROADMAP §C reserves no migrations"
    assert reserved == list(range(reserved[0], reserved[-1] + 1)), (
        f"ROADMAP migration reservations are not contiguous: {reserved}"
    )

    shipped = {rev for _u, s, revs in records if s == "shipped" for rev in revs}
    pending = sorted(rev for _u, s, revs in records if s == "pending" for rev in revs)
    pending_set = set(pending)
    # shipped/pending must partition every reserved revision (disjoint + exhaustive) —
    # a second, explicit statement of the fail-closed State contract above.
    assert shipped.isdisjoint(pending_set), (
        f"revisions marked both shipped and pending: {sorted(shipped & pending_set)}"
    )
    assert shipped | pending_set == set(flat), (
        f"revisions owned by neither shipped nor pending: {sorted(set(flat) - shipped - pending_set)}"
    )

    # every authored revision at/above the first reservation must be owned by a shipped
    # row, and every shipped revision must be authored — equality catches an authored
    # revision still marked pending (the round-3 ownership collision).
    authored_reserved = {rev for rev in existing if rev >= reserved[0]}
    assert authored_reserved == shipped, (
        f"authored reserved revisions {sorted(authored_reserved)} != shipped-owned "
        f"{sorted(shipped)}; a migration is authored but still marked pending (or vice versa)"
    )
    # no pending revision may already exist in the Alembic chain
    collided = sorted(pending_set & existing)
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


# --- mutation resistance: crafted §C tables are driven through the REAL parser so a
#     parser regression (dedup, shifted column) is caught too, then validated against a
#     fixed baseline (revisions 001..012 authored, single head 012). ------------------

_HEADER = (
    "| Unit | Item(s) | State | Migration | Content |\n"
    "|------|---------|-------|-----------|---------|\n"
)
_EXISTING = set(range(1, 13))  # revisions 001..012 authored
_HEAD = 12

_REAL_BODY = [
    "| PR 2 | 2 | shipped | 008 | x |",
    "| PR 4 | 5 | shipped | 009 | x |",
    "| PR 5a | 6 | shipped | 010 | x |",
    "| PR 6 | 7A | shipped | 011, 012 | x |",
    "| PR 6b | 7B | pending | 013 | x |",
    "| PR 7a | 9 | pending | 014 | x |",
    "| PR 7b | 8 | pending | 015 | x |",
    "| PR 8 | 10 | pending | 016 | x |",
    "| PR 10 | 13 | pending | 017 | x |",
]


def _validate_body(body_rows: list[str]) -> None:
    text = _HEADER + "\n".join(body_rows) + "\n"
    _validate_lineage(_parse_roadmap_records(text), _EXISTING, _HEAD)


def test_parser_and_validator_accept_real_shape():
    _validate_body(_REAL_BODY)


def test_validator_rejects_ownership_collision():
    # gap-free, but PR 6 (shipped) owns only 011 and the PENDING PR 6b reserves 012 —
    # which is already authored. The ownership equality / head-shipped checks reject it.
    with pytest.raises(AssertionError):
        _validate_body([
            "| PR 2 | 2 | shipped | 008 | x |",
            "| PR 4 | 5 | shipped | 009 | x |",
            "| PR 5a | 6 | shipped | 010 | x |",
            "| PR 6 | 7A | shipped | 011 | x |",
            "| PR 6b | 7B | pending | 012 | x |",
            "| PR 7a | 9 | pending | 013 | x |",
            "| PR 7b | 8 | pending | 014 | x |",
            "| PR 8 | 10 | pending | 015 | x |",
            "| PR 10 | 13 | pending | 016 | x |",
        ])


def test_validator_rejects_duplicate_reservation():
    # two pending rows book 013; the numeric RANGE stays gap-free (013..017 present),
    # so only the pre-dedup duplicate check (through the real parser) can catch it.
    with pytest.raises(AssertionError):
        _validate_body([
            "| PR 2 | 2 | shipped | 008 | x |",
            "| PR 4 | 5 | shipped | 009 | x |",
            "| PR 5a | 6 | shipped | 010 | x |",
            "| PR 6 | 7A | shipped | 011, 012 | x |",
            "| PR 6b | 7B | pending | 013 | x |",
            "| PR 7a | 9 | pending | 013 | x |",
            "| PR 7b | 8 | pending | 014 | x |",
            "| PR 8 | 10 | pending | 015 | x |",
            "| PR 10 | 13 | pending | 016, 017 | x |",
        ])


def test_validator_rejects_unknown_state():
    # PR 7a's State is the typo 'pendng' while it still reserves 014 — must fail closed,
    # not silently orphan 014 from both ownership sets (round-4 finding).
    with pytest.raises(AssertionError):
        _validate_body([r.replace("| pending | 014 |", "| pendng | 014 |") for r in _REAL_BODY])


def test_validator_rejects_numeric_gap():
    # drop PR 7a's 014 entirely, leaving 013 then 015 → non-contiguous reservation.
    with pytest.raises(AssertionError):
        _validate_body([r for r in _REAL_BODY if not r.startswith("| PR 7a |")])


def test_validator_rejects_stateful_row_without_migration():
    # a row with no migration must be State '—'; a stray 'pending' on it fails closed.
    with pytest.raises(AssertionError):
        _validate_body([*_REAL_BODY, "| PR 5b | 11 | pending | — | x |"])
