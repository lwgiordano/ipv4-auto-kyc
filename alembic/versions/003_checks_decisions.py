"""checks (append-only, supersession, live partial unique) and decisions

Revision ID: 003
Revises: 002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("check_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("points_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_detail_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("reason_codes", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_by_run_id", sa.Text(), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("superseded_by_check_id", sa.Text(), sa.ForeignKey("checks.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_checks_case_id", "checks", ["case_id"])
    # AUDIT:D3 — at most one live check per (case, check_type)
    op.create_index(
        "uq_checks_live_per_type",
        "checks",
        ["case_id", "check_type"],
        unique=True,
        postgresql_where=sa.text("superseded_by_check_id IS NULL"),
    )
    op.create_table(
        "decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("gates_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("buy_enablement", sa.Text(), nullable=False),
        sa.Column("policy_shas", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reviewer_id", sa.Text(), nullable=True),
    )
    op.create_index("ix_decisions_case_id", "decisions", ["case_id"])


def downgrade() -> None:
    op.drop_table("decisions")
    op.drop_index("uq_checks_live_per_type", table_name="checks")
    op.drop_table("checks")
