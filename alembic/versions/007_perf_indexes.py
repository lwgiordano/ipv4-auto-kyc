"""performance indexes for hot read/claim paths

Revision ID: 007
Revises: 006

Adds supporting indexes for the queries that grow with retention and run on
every ops-console poll (audit feed, latest decision, adapter latency) and for
the queue-claim per-case FIFO subquery, which otherwise scans a case's whole
job history. Pure additive DDL; up/down clean.
"""

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # case audit feed: WHERE case_id=? ORDER BY id DESC LIMIT N → bounded index scan
    op.create_index("ix_audit_log_case_id_id", "audit_log", ["case_id", "id"])
    # queue claim per-case FIFO subquery: probe only OPEN jobs, not done history
    op.create_index(
        "ix_jobs_case_open",
        "jobs",
        ["case_id", "id"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    # adapter latency / metrics windowing
    op.create_index("ix_adapter_results_fetched_at", "adapter_results", ["fetched_at"])
    # latest-decision-per-case lookup (get_case ORDER BY decided_at DESC LIMIT 1)
    op.create_index("ix_decisions_case_decided", "decisions", ["case_id", "decided_at"])


def downgrade() -> None:
    op.drop_index("ix_decisions_case_decided", table_name="decisions")
    op.drop_index("ix_adapter_results_fetched_at", table_name="adapter_results")
    op.drop_index("ix_jobs_case_open", table_name="jobs")
    op.drop_index("ix_audit_log_case_id_id", table_name="audit_log")
