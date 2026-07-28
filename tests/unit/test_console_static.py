"""Static pins on the ops console's embedded JS (re-audit 15d875d F6).

The case-detail verdict tuple must come from `pointer_decision` — the trigger-maintained
authority row — and never regress to `decisions[0]` (decided_at display order inverts against
commit order) or to the stale `case.latest_decision` projection column. The console is a single
static file with no JS test harness, so the contract is pinned at the source level.
"""

from pathlib import Path

CONSOLE = (
    Path(__file__).resolve().parents[2] / "src" / "kyc_tool" / "ui" / "console.html"
).read_text(encoding="utf-8")


def test_case_detail_reads_the_pointer_row():
    assert "d.pointer_decision" in CONSOLE


def test_display_ordered_decisions_never_pick_the_verdict():
    assert "decisions[0]" not in CONSOLE


def test_stale_projection_column_is_not_the_detail_authority():
    # the LIST pill's `c.latest_decision` is fine — routes.list_cases serves that field from
    # the pointed row — but the DETAIL view must never read it off the case object
    assert "d.case.latest_decision" not in CONSOLE
    assert "c.latest_decision||{}" not in CONSOLE
