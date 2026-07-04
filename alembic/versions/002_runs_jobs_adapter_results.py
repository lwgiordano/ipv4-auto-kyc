"""runs, jobs (queue), adapter_results

Revision ID: 002
Revises: 001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("triggering_event_id", sa.Text(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="QUEUED"),
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("policy_bundle_hash", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_runs_case_id", "runs", ["case_id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=True),
        sa.Column("payload_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_after"])
    op.create_index("ix_jobs_case_id", "jobs", ["case_id"])
    op.create_table(
        "adapter_results",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("adapter_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("normalized_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("input_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.UniqueConstraint("run_id", "adapter_id", "input_hash", name="uq_adapter_run_input"),
    )
    op.create_index("ix_adapter_results_run_id", "adapter_results", ["run_id"])


def downgrade() -> None:
    op.drop_table("adapter_results")
    op.drop_table("jobs")
    op.drop_table("runs")
