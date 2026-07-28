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


# --- cross-artifact migration parity (re-audit `4dfdf8a` F6) ---
# Number continuity in the §C table alone is insufficient: the §C row said `015` while its own
# Content cell still read `down_revision='013'`, and the activation/6b specs each carried a third
# opinion. One revision number must mean one thing across every artifact that names it.

_SPECS = REPO_ROOT / ".agents" / "superpowers" / "specs"


def _roadmap_text() -> str:
    return (REPO_ROOT / ".agents" / "ROADMAP.md").read_text()


def test_roadmap_detail_sections_chain_contiguously():
    """Every detailed `Migration **NNN** (`down_revision='MMM'`)` section must chain M = N-1 —
    a section claiming a down_revision that skips the repair revisions is exactly how the split
    lineage read as consistent while bypassing 014/015."""
    pairs = re.findall(r"Migration \*\*(\d{3})\*\* \(`down_revision='(\d{3})'`\)", _roadmap_text())
    assert pairs, "no detailed migration sections found — the parser regressed"
    for rev, down in pairs:
        assert int(down) == int(rev) - 1, (
            f"ROADMAP detail section: migration {rev} claims down_revision={down}; "
            f"the chain is contiguous, so it must be {int(rev) - 1}"
        )


def test_roadmap_table_and_detail_sections_agree():
    """A revision reserved in a §C row must appear as that unit's detailed section number too —
    the table said 015 while the detail heading still said 014's content."""
    text_ = _roadmap_text()
    activation_row = re.search(r"\| PR 7b-activation \| 8 \| pending \| (\d{3}) \|", text_)
    assert activation_row, "activation row missing from §C"
    detail = re.search(
        r"### PR 7b-activation[^\n]*\nMigration \*\*(\d{3})\*\*", text_
    )
    assert detail, "activation detail section missing"
    assert activation_row.group(1) == detail.group(1), (
        f"§C reserves {activation_row.group(1)} for 7b-activation but the detail section says "
        f"{detail.group(1)}"
    )
    # and the row's own Content cell must not carry a contradicting down_revision
    row_line = next(line for line in text_.splitlines() if line.startswith("| PR 7b-activation "))
    inner = re.search(r"down_revision='(\d{3})'", row_line)
    if inner:
        assert int(inner.group(1)) == int(activation_row.group(1)) - 1, (
            f"the activation ROW text says down_revision='{inner.group(1)}' but the reserved "
            f"revision is {activation_row.group(1)} — the cell contradicts its own row"
        )


def test_activation_spec_agrees_with_roadmap():
    spec = (_SPECS / "2026-07-22-pr7b-activation-platform-ordering-design.md").read_text()
    m = re.search(r"this doc, migration `(\d{3})`, `down_revision='(\d{3})'`", spec)
    assert m, "activation spec no longer declares its migration in the header"
    row = re.search(r"\| PR 7b-activation \| 8 \| pending \| (\d{3}) \|", _roadmap_text())
    assert m.group(1) == row.group(1), (
        f"activation spec says migration {m.group(1)}; ROADMAP §C reserves {row.group(1)}"
    )
    assert int(m.group(2)) == int(m.group(1)) - 1


def test_6b_spec_is_banner_superseded_not_silently_stale():
    """The paused 6b spec self-assigned migration 013 and 'lands before 7b'. Until it is
    re-planned, the superseding banner must be present and must name the current chain."""
    spec = (_SPECS / "2026-07-22-pr6b-revalidation-design.md").read_text()
    assert "PAUSED / SUPERSEDED ORDERING" in spec
    row = re.search(r"\| PR 6b \| 7B \| pending \| (\d{3}) \|", _roadmap_text())
    assert row, "6b row missing from §C"
    assert f"migration `{row.group(1)}`" in spec, (
        f"the 6b banner must name the currently reserved revision {row.group(1)}"
    )


# A revision-history section records what a document USED to say; everything above it is live
# guidance. Scanning only a byte prefix let contradictions survive further down the file — the
# activation spec's "Out of scope" section called 7b-core "not yet shipped" through three green
# audit rounds (re-audit `cbb783b` F8). Live text is now scanned in FULL, to this boundary.
_REVISION_HISTORY = re.compile(r"^#{1,3}\s*Revision note", re.MULTILINE)


def _live_section(text: str) -> str:
    """Everything before the first revision-history heading: the document's LIVE claims."""
    marker = _REVISION_HISTORY.search(text)
    return text[: marker.start()] if marker else text


def test_activation_spec_status_matches_roadmap_state():
    """Re-audit 0c46443 F7 / `cbb783b` F8: the activation spec must not describe 7b-core as
    pending anywhere in its LIVE text while the ROADMAP marks its revisions shipped — status is
    stated once, everywhere. Historical notes below the revision-history boundary are exempt."""
    spec = (_SPECS / "2026-07-22-pr7b-activation-platform-ordering-design.md").read_text()
    live = _live_section(spec)
    for stale in ("pending/planned", "not yet shipped", "not shipped yet"):
        hits = [
            f"line {i}: {line.strip()}"
            for i, line in enumerate(live.splitlines(), 1)
            if stale in line.lower()
        ]
        assert not hits, (
            f"the activation spec still describes 7b-core as {stale!r} in live text; "
            f"ROADMAP §C says shipped\n" + "\n".join(hits)
        )
    assert "SHIPPED" in spec[:1200]
