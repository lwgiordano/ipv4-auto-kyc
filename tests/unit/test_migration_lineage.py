"""Migration-lineage guard (audit round 2).

Adding the validation revision 012 (audit round 1's P1 fix) consumed the migration
number the ROADMAP still reserved for PR 6b — a real forward collision: the next
migration PR, authored from the canonical plan, would have reused revision id `012`
or branched again from `011`, producing a duplicate-revision / multiple-head Alembic
graph that breaks the migration CI gate and `/readyz`'s single-head lookup.

These tests keep the ROADMAP's `§C` migration reservations consistent with the live
Alembic chain so that class of collision fails CI *before* another migration is
authored. They read the migration scripts and the ROADMAP off disk — no database.
"""

import re

from alembic.config import Config
from alembic.script import ScriptDirectory

from kyc_tool.config import REPO_ROOT


def _script() -> ScriptDirectory:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _roadmap_reserved_migrations() -> list[int]:
    """The zero-padded revision ids reserved in the ROADMAP `§C` table's Migration
    column (rows begin `| PR ... |`; the 3rd cell holds `011`, or `011, 012`, or `—`)."""
    text = (REPO_ROOT / ".agents" / "ROADMAP.md").read_text()
    nums: set[int] = set()
    for line in text.splitlines():
        if not line.lstrip().startswith("| PR "):  # skips header/separator/other rows
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        for tok in re.findall(r"\b0\d\d\b", cells[2]):  # the Migration column only
            nums.add(int(tok))
    return sorted(nums)


def test_single_alembic_head():
    heads = _script().get_heads()
    assert len(heads) == 1, f"expected exactly one Alembic head, got {heads}"


def test_roadmap_reservations_extend_the_live_chain():
    script = _script()
    existing = {int(r.revision) for r in script.walk_revisions()}
    head_num = int(script.get_heads()[0])
    reserved = _roadmap_reserved_migrations()

    assert reserved, "ROADMAP §C must reserve migration numbers"
    # unique + contiguous: the reservation table must not double-book or skip a number
    assert reserved == list(range(reserved[0], reserved[-1] + 1)), (
        f"ROADMAP migration reservations are not contiguous/unique: {reserved}"
    )
    # the live head must be an accounted-for reservation
    assert head_num in reserved, (
        f"live Alembic head {head_num:03d} is not reserved in the ROADMAP {reserved}"
    )
    # the first reservation ABOVE the head is the next migration to author: it must be
    # exactly head+1 and must NOT already be an authored revision (the audit round-2
    # collision — validation 012 consuming the number reserved for the next PR).
    above = [n for n in reserved if n > head_num]
    assert above, f"ROADMAP reserves no migration beyond the live head {head_num:03d}"
    assert above[0] == head_num + 1, (
        f"next reserved migration {above[0]:03d} != head+1 ({head_num + 1:03d}); "
        "renumber the ROADMAP reservations after adding a migration"
    )
    assert above[0] not in existing, (
        f"next reserved migration {above[0]:03d} already exists in the Alembic chain"
    )
