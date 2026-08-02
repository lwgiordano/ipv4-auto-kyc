"""PR 10a: RUNBOOK repair governance. Hand-written UPDATE jobs/outbox SQL is not a sanctioned
operator path — the jobs variant skipped the run reset + audit record, and the outbox variant
(post-7b-core) left the claim tuple and bypassed the 018+ transition authority. The RUNBOOK must
point at the authenticated console requeue endpoints instead, and both endpoints must exist."""

import re

from kyc_tool.config import REPO_ROOT


def test_runbook_contains_no_raw_requeue_update_sql():
    """REQUEUE-shaped SQL (SET status back to pending/queued) is the forbidden family. The deliberate
    retire-a-redacted-row block (SET status='dead') stays: no endpoint exists for it and the 018+
    authority permits only that direction for a redacted body."""
    body = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()
    assert not re.search(r"UPDATE\s+(jobs|outbox)\s+SET\s+status\s*=\s*'(pending|queued)'", body), (
        "RUNBOOK reintroduced a hand-SQL requeue — use the console endpoints"
    )


def test_runbook_points_at_both_console_requeue_endpoints():
    body = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()
    assert "/v1/ops/requeue/job/{id}" in body
    assert "/v1/ops/requeue/outbox/{id}" in body


def test_the_documented_endpoints_exist_in_the_always_mounted_ops_router():
    source = (REPO_ROOT / "src/kyc_tool/api/routes_ops.py").read_text()
    assert '/v1/ops/requeue/job/{job_id}' in source
    assert '/v1/ops/requeue/outbox/{outbox_id}' in source
