"""Re-audit `d3c0852..23e005e` F4 + `d569a15..4938840` F5/F7 + `8aba2df..2cee937` R3-F5/F6 +
`5b0f0b8..b75a320` R4-F5: the outbox retry ceiling is PROCESS-LOCAL, so ANY change to
`KYC_OUTBOX_MAX_ATTEMPTS` — raise OR lower — is a drained publisher cutover, never a rolling restart.

The rule is a CLOSED phase machine (`kyc_tool.ops.cutover`). `validate_cutover` requires the exact
ordered actions, so deleting the stop step, dropping the new-value attestation, reordering, or a
"start no publishers" phase are all refused — none is a valid machine. The operator surfaces embed the
block RENDERED from the record; the guard extracts each surface's marked block and compares it to the
render, so the docs are derived, not matched by file-wide token presence.
"""

import dataclasses

from kyc_tool.config import REPO_ROOT
from kyc_tool.ops.cutover import OUTBOX_MAX_ATTEMPTS_CUTOVER as CUT
from kyc_tool.ops.cutover import (
    CutoverAction,
    CutoverPhase,
    extract_block,
    render_cutover,
    validate_cutover,
)

_SURFACES = ("docs/DEPLOYMENT.md", "docs/RUNBOOK.md", ".env.example")
_FALSE_CLAIMS = (
    "raising the ceiling is always safe",
    "raising is always safe",
    "raising it is rolling-safe",
    "raising is rolling-safe",
)


def test_canonical_record_is_a_valid_closed_machine():
    assert validate_cutover(CUT) == []
    assert tuple(p.action for p in CUT.phases) == (
        CutoverAction.DISABLE_RESTART, CutoverAction.STOP, CutoverAction.ATTEST_ZERO,
        CutoverAction.ATTEST_NEW_VALUE, CutoverAction.START,
    )


def _without(action):
    return dataclasses.replace(CUT, phases=tuple(p for p in CUT.phases if p.action is not action))


def test_validate_rejects_every_corruption_through_the_real_consumer():
    """The exact mutations Codex slipped past the string-search validator must now each fail."""
    swapped = list(CUT.phases)
    swapped[1], swapped[2] = swapped[2], swapped[1]  # attest_zero before stop
    mutations = {
        "single direction": dataclasses.replace(CUT, both_directions=False),
        "deleted stop step": _without(CutoverAction.STOP),
        "deleted new-value attestation": _without(CutoverAction.ATTEST_NEW_VALUE),
        "zero-attest moved before stop": dataclasses.replace(CUT, phases=tuple(swapped)),
        "start no publishers": dataclasses.replace(
            CUT,
            phases=CUT.phases[:-1] + (CutoverPhase(CutoverAction.START, roles=()),),
        ),
        "duplicated start": dataclasses.replace(CUT, phases=CUT.phases + (CUT.phases[-1],)),
        "missing dev_worker role": dataclasses.replace(CUT, publisher_roles=("outbox_worker",)),
        "aliased role": dataclasses.replace(
            CUT, publisher_roles=("outbox_worker", "outbox_worker", "dev_worker")
        ),
        "wrong setting attested": dataclasses.replace(
            CUT,
            phases=tuple(
                dataclasses.replace(p, setting="KYC_SOMETHING_ELSE")
                if p.action is CutoverAction.ATTEST_NEW_VALUE else p
                for p in CUT.phases
            ),
        ),
    }
    for label, mutated in mutations.items():
        assert validate_cutover(mutated), f"validate_cutover accepted a corrupted machine: {label}"


def test_every_surface_embeds_the_block_rendered_from_the_record():
    """Each surface's marked block, extracted and normalized, equals render_cutover(record) exactly —
    a garbled/edited step in any surface fails here, and so does drift in the record itself."""
    expected = render_cutover(CUT)
    for rel in _SURFACES:
        block = extract_block((REPO_ROOT / rel).read_text(), CUT)
        assert block == expected, f"{rel} block != render_cutover(record)"


def test_the_disable_restart_step_is_present_in_every_surface():
    """R4-F5: .env.example previously omitted the disable-restart fence; it is step 1 of the rendered
    block now embedded in all three surfaces."""
    for rel in _SURFACES:
        assert "disable autoscaling and rolling restart" in (REPO_ROOT / rel).read_text()


def test_no_surface_reasserts_the_false_raising_is_safe_claim():
    for rel in (*_SURFACES, "src/kyc_tool/config.py"):
        body = (REPO_ROOT / rel).read_text().lower()
        hits = [c for c in _FALSE_CLAIMS if c in body]
        assert not hits, f"{rel} reasserts a refuted claim {hits}"
