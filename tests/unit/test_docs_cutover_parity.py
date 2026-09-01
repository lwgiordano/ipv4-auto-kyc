"""The PR 7b-core cutover/rollback procedure must be byte-identical in RUNBOOK.md and
DEPLOYMENT.md (only the section-header line may differ), including wrapped continuation lines."""

from pathlib import Path

from kyc_tool.config import REPO_ROOT

_HEADING = "PR 7b-core cutover"


def _body(path: Path) -> str:
    """Section body from the '## ... PR 7b-core cutover ...' heading (EXCLUSIVE of the heading
    line) to the next top-level '## ' — every wrapped continuation line included."""
    lines = path.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## ") and _HEADING in ln)
    out = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        out.append(ln)
    return "\n".join(out).strip("\n")


def test_runbook_and_deployment_cutover_bodies_identical():
    rb = _body(REPO_ROOT / "docs" / "RUNBOOK.md")
    dp = _body(REPO_ROOT / "docs" / "DEPLOYMENT.md")
    assert rb == dp  # FULL bodies identical (mutating any continuation line in either fails this)
    for token in (
        "restore from authoritative backup", "BLOCKED_NO_AUTHORITATIVE_MAPPING",
        "zero at the orchestrator", "reset_interrupted_outbox_claims",
        "python -m alembic -c alembic.ini downgrade 012", "R6.",
        # both rollback branches present AND both end in a full resume (no ending-stopped, no
        # frozen retention) — the F1 completeness the earlier first-line-only diff missed:
        "ROLLBACK OUTCOME A", "ROLLBACK OUTCOME B", "PROHIBIT the pre-7b image",
        "re-enable retention, autoscaling/restarts, and submissions",
        "remove the composer edge block",
        # re-audit F1 — the restore acceptance contract is executable, not advisory:
        "RESTORE ACCEPTANCE CONTRACT", "original_outbox_id",
        "default-id INSERT is prohibited", "ACCEPTANCE PREDICATE",
        "every schema-012 `outbox` column", "o.attempts = :original_attempts",
        "o.created_at IS NOT DISTINCT FROM :original_created_at",
        # the predicate is POSITIVE (re-review 0ca264b P1/P2):
        "MUST return", "EXACTLY ONE row", "ZERO rows = still blocked",
        # re-audit `8377440` F3 — the sequence is the restore CLI's job; NO circular precondition,
        # and `repair_outbox_sequence` is the SEPARATE case (divergent high-water, no row):
        "THE SEQUENCE IS THE RESTORE CLI'S JOB", "there is NO separate precondition",
        "python -m kyc_tool.ops.repair_outbox_sequence",
        "SEPARATE DRAINED action", "ONLY case the restore does not cover",
        "substituting `now()` for `delivered_at` is prohibited",
        # re-audit `f495de8` F1 / `8377440` F3 — SHIPPED bounded CLI, floors in ONE transaction:
        "restore_pr7b_core_callback", "--expect-original-id",
        # re-audit `538e55e..42e1c7d` F2 — the out-of-band manifest anchor is MANDATORY and named:
        "--expect-manifest-digest", "signed", "the file cannot self-certify",
        # re-audit `538e55e..42e1c7d` F12 — the read-only ops-prerequisite preflight runs first:
        "verify_pr7b_ops_prerequisites",
        "GREATEST(max(id), original_id) + 1", "pre-window maintenance stop",
        "Pasting the SQL below by hand is NOT a sanctioned path",
    ):
        assert token in rb  # safety-critical details survive, not just the numbered leaders
    assert "setval(pg_get_serial_sequence" not in rb  # the live sequence write must stay deleted
    assert "RESTART WITH <" not in rb  # no pseudocode placeholder survives into the runbook
    # F3: the circular "confirm next_id > original before restoring" gate must be GONE
    assert "confirm the id the sequence would hand the next writer is already past it" not in rb
    assert rb.count("edge-block the composer") == 2  # forward + rollback both establish the fence
    assert rb.count("remove the composer edge block") == 3  # forward + both rollback outcomes clear it
