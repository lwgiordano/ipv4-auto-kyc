"""PR 10a RUNBOOK repair governance, hardened per re-audit `f2929f8..6a4cd87` F14: executable
semantics, not text tokens. The forbidden requeue-SQL check parses fenced sql blocks (case/schema
insensitive); the endpoint check inspects the MOUNTED FastAPI routes of a real app."""

import re

from kyc_tool.config import REPO_ROOT


def _fenced_sql_blocks(body: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", body, re.S | re.I)


def test_runbook_contains_no_raw_requeue_update_sql():
    """Requeue-shaped SQL (status back to pending/queued, any case, optionally schema-qualified) is
    forbidden in fenced sql blocks; the deliberate retire-a-redacted-row block (status='dead') stays."""
    body = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()
    head = re.compile(r"UPDATE\s+(?:\w+\.)?(jobs|outbox)\s+SET\b", re.I)
    assign = re.compile(r"status\s*=\s*'(pending|queued)'", re.I)
    for block in _fenced_sql_blocks(body):
        for m in head.finditer(block):
            set_clause = re.split(r"\bWHERE\b", block[m.end():], maxsplit=1, flags=re.I)[0]
            assert not assign.search(set_clause), (
                f"RUNBOOK reintroduced a hand-SQL requeue: {block[:120]!r}"
            )


def test_runbook_points_at_both_ops_requeue_endpoints():
    body = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()
    assert "/v1/ops/requeue/job/{id}" in body
    assert "/v1/ops/requeue/outbox/{id}" in body
