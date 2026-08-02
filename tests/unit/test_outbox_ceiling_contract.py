"""Re-audit `d3c0852..23e005e` F4 + `d569a15..4938840` F5/F7 + `8aba2df..2cee937` R3-F5/F6: the
outbox retry ceiling is PROCESS-LOCAL, so ANY fleet-wide change to `KYC_OUTBOX_MAX_ATTEMPTS` — raise
OR lower — is a drained publisher cutover, never a rolling restart.

The rule lives in ONE machine-readable record (`kyc_tool.ops.cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER`).
This guard validates it through the REAL consumer (`validate_cutover`) and mutation-tests that
consumer — a rewrite that flips the rule while preserving keywords (e.g. "raising may use a rolling
restart") is caught because the mutation corrupts the STRUCTURED record, not file-wide tokens. The
operator surfaces (DEPLOYMENT, RUNBOOK, .env.example) must mechanically match the record's load-bearing
fields, and none may reassert the refuted "raising is safe" claim.
"""

import dataclasses

from kyc_tool.config import REPO_ROOT
from kyc_tool.ops.cutover import OUTBOX_MAX_ATTEMPTS_CUTOVER as CUT
from kyc_tool.ops.cutover import validate_cutover

_FALSE_CLAIMS = (
    "raising the ceiling is always safe",
    "raising is always safe",
    "raising it is rolling-safe",
    "raising is rolling-safe",
    "raising the ceiling is safe",
)


def test_canonical_cutover_record_is_valid():
    assert validate_cutover(CUT) == []
    assert CUT.both_directions is True
    assert "outbox_worker" in CUT.publisher_roles and "dev_worker" in CUT.publisher_roles
    assert CUT.steps[-1].lower().startswith("start")  # start is last, after attestation


def test_validate_cutover_rejects_each_corruption_through_the_real_consumer():
    """Mutation self-test through `validate_cutover`, NOT token search: every flip of the rule
    produces at least one violation."""
    mutations = {
        "single-direction (raise-safe / lower-safe)": dataclasses.replace(CUT, both_directions=False),
        "rolling restart allowed": dataclasses.replace(CUT, disable_autoscale=False),
        "stale task definition allowed": dataclasses.replace(CUT, attest_new_value=False),
        "missing dev_worker role": dataclasses.replace(CUT, publisher_roles=("outbox_worker",)),
        "aliased/duplicated role": dataclasses.replace(
            CUT, publisher_roles=("outbox_worker", "outbox_worker", "dev_worker")
        ),
        "start before the zero attestation": dataclasses.replace(
            CUT,
            steps=(
                "disable autoscaling and rolling restart",
                "start publishers on the new value",
                "stop ALL outbox publishers of every role",
                "attest zero publishers are running",
                "attest every new task definition carries the exact new value",
            ),
        ),
        "omitted disable-autoscale step": dataclasses.replace(
            CUT, steps=tuple(s for s in CUT.steps if "disable" not in s.lower())
        ),
    }
    for label, mutated in mutations.items():
        assert validate_cutover(mutated), f"validate_cutover accepted a corrupted record: {label}"


def test_operator_surfaces_mechanically_match_the_record():
    """DEPLOYMENT, RUNBOOK and .env.example must reflect the record's load-bearing fields — checked
    against the STRUCTURED record, not free tokens."""
    surfaces = {
        "docs/DEPLOYMENT.md": (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text().lower(),
        "docs/RUNBOOK.md": (REPO_ROOT / "docs" / "RUNBOOK.md").read_text().lower(),
        ".env.example": (REPO_ROOT / ".env.example").read_text().lower(),
    }
    for name, body in surfaces.items():
        assert CUT.setting.lower() in body, f"{name} omits {CUT.setting}"
        assert "drained" in body, f"{name} omits the drained-cutover rule"
        assert ("raise" in body or "raising" in body) and (
            "lower" in body or "lowering" in body
        ), f"{name} does not state BOTH directions need the cutover"
        for role in CUT.publisher_roles:
            assert role in body, f"{name} does not name the {role} publisher role"


def test_no_surface_reasserts_the_false_raising_is_safe_claim():
    for rel in ("docs/DEPLOYMENT.md", "docs/RUNBOOK.md", ".env.example", "src/kyc_tool/config.py"):
        body = (REPO_ROOT / rel).read_text().lower()
        hits = [c for c in _FALSE_CLAIMS if c in body]
        assert not hits, f"{rel} reasserts a refuted claim {hits}: raising also needs a drained cutover"
    assert "raising is always safe" in _FALSE_CLAIMS  # the guard has teeth
