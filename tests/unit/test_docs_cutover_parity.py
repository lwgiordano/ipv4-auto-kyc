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
        "pg_get_serial_sequence('outbox','id')",
        # re-review 0ca264b P1/P2 — the predicate is POSITIVE and the sequence is READ-ONLY:
        "MUST return", "EXACTLY ONE row", "ZERO rows = still blocked",
        "SEQUENCE PRECONDITION", "`setval(...)` is prohibited on this path",
        "SEPARATE DRAINED action", "python -m kyc_tool.ops.repair_outbox_sequence",
        "LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE",
        "COALESCE(max(id), 0) + 1",
        # the repair is the SHIPPED fail-closed CLI, not a psql block that exits 0 on a
        # false boolean. Assert the CLI and its readback contract, not the deleted recipe:
        "repair_outbox_sequence: OK", "repair_outbox_sequence: FAILED",
        "exit status IS the result",
        "substituting `now()` for `delivered_at` is prohibited",
    ):
        assert token in rb  # safety-critical details survive, not just the numbered leaders
    assert "setval(pg_get_serial_sequence" not in rb  # the live sequence write must stay deleted
    assert "RESTART WITH <" not in rb  # no pseudocode placeholder survives into the runbook
    assert rb.count("edge-block the composer") == 2  # forward + rollback both establish the fence
    assert rb.count("remove the composer edge block") == 3  # forward + both rollback outcomes clear it
