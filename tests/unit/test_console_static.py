"""Static pins on the ops console's embedded JS (re-audits `15d875d` F6, `cbb783b` F6).

The console is a single static file with no JS test harness, so the DOM's authority expressions
are pinned at the source level. Whole-file substring presence is NOT enough — it passes when the
right token exists anywhere and the wrong one exists in the function that matters — so every
assertion here is scoped to the body of the specific renderer under test.
"""

import re
from pathlib import Path

CONSOLE = (
    Path(__file__).resolve().parents[2] / "src" / "kyc_tool" / "ui" / "console.html"
).read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """The source of one `async function <name>(...)`, from its header to the next top-level
    `async function` (the file's renderers are all declared at column 0)."""
    start = re.search(rf"^async function {name}\(", CONSOLE, re.MULTILINE)
    assert start, f"renderer {name}() not found — the pin is scoped to a function that moved"
    rest = CONSOLE[start.end():]
    end = re.search(r"^async function ", rest, re.MULTILINE)
    body = rest[: end.start()] if end else rest
    # `//` comments are stripped: a line RECORDING a correction ("… c.latest_decision goes stale
    # …") is the fix, not a reassertion of it, and must not trip a ban on the expression itself.
    return "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())


VIEW_CASE = _function_body("viewCase")
VIEW_CASES = _function_body("viewCases")


# --- the verdict tuple comes from the pointer row, in the renderer that draws it --------------

def test_case_detail_reads_the_pointer_row():
    assert "d.pointer_decision" in VIEW_CASE


def test_case_detail_never_picks_a_verdict_from_display_ordered_decisions():
    """`decisions[]` is decided_at-ordered for display; decided_at is transaction-start time and
    inverts against commit order, so it may never select the verdict."""
    assert "decisions[0]" not in VIEW_CASE
    assert not re.search(r"\bd\.decisions\s*\[", VIEW_CASE)


def test_case_detail_never_reads_the_stale_projection_column():
    assert "c.latest_decision" not in VIEW_CASE


# --- live evidence and the published decision are visibly different things --------------------

def test_case_detail_labels_live_evidence_as_not_the_decision():
    assert "current_evidence_score" in VIEW_CASE
    assert "Live evidence — not the published decision" in VIEW_CASE


def test_case_detail_decision_card_sources_the_whole_tuple_from_one_row():
    """decision, score and buy enablement all read off `latest` (the pointed row) — never off
    the case projection or the live score (re-audit `cbb783b` F6)."""
    card = VIEW_CASE[VIEW_CASE.index("Published decision"):]
    card = card[: card.index("Live evidence")]
    for expr in ("latest.decision", "latest.score", "latest.buy_enablement"):
        assert expr in card, f"the published-decision card must render {expr}"
    assert "Published decision hard gates" in card
    assert "published-gates" in card
    assert "gates[g0]" in card
    assert "current_score" not in card and "current_evidence_score" not in card
    assert '<h2 class="sec">Hard gates' not in VIEW_CASE


def test_live_bar_is_not_coloured_by_the_published_decision():
    """A neutral bar is the point: colouring live points by the published verdict merges two
    authority eras into one visual read."""
    bar = VIEW_CASE[VIEW_CASE.index("bmeasure"):]
    bar = bar[: bar.index("btick")]
    assert "var(--faint)" in bar
    assert "decisionColor" not in bar and "barColor" not in bar


# --- the list shows both numbers, each under its own name ------------------------------------

def test_case_list_separates_decision_score_from_live_evidence():
    assert "c.decision_score" in VIEW_CASES
    assert "c.current_evidence_score" in VIEW_CASES
    # the blended field the list used to render under a single "Score" column
    assert "c.current_score" not in VIEW_CASES
