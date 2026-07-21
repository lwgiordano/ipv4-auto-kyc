"""policy bundle pinning: policy_bundles + bundle_pinning_epoch + provenance cols

Revision ID: 011
Revises: 010

Adds the durable policy-bundle store (policy_bundles, keyed by content hash),
the single-row activation epoch (bundle_pinning_epoch) that pins the currently
enforced bundle + engine build, and three NULLABLE provenance columns
recording which bundle/engine build produced each row: checks.policy_bundle_hash,
runs.engine_build_id, decisions.engine_build_id.

ADDITIVE + HOT-COMPATIBLE: every new column is nullable and both new tables are
brand new, so an old replica still running the prior image is unaffected during
a rolling deploy.

Downgrade is FORWARD-ONLY after use: once a bundle/epoch row exists, or any
check/run/decision has recorded pinning provenance, those rows are immutable
audit evidence — downgrade refuses loudly rather than deleting them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_bundles",
        sa.Column("bundle_hash", sa.Text(), primary_key=True),
        sa.Column("files_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "bundle_pinning_epoch",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "bundle_hash",
            sa.Text(),
            sa.ForeignKey("policy_bundles.bundle_hash", name="fk_epoch_bundle_hash"),
            nullable=False,
        ),
        sa.Column("engine_build_id", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_epoch_id"),
        sa.CheckConstraint("btrim(engine_build_id) <> ''", name="ck_epoch_engine_nonblank"),
    )

    op.add_column("checks", sa.Column("policy_bundle_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_checks_policy_bundle_hash_nonblank",
        "checks",
        "policy_bundle_hash IS NULL OR btrim(policy_bundle_hash) <> ''",
    )

    op.add_column("runs", sa.Column("engine_build_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_runs_engine_build_id_nonblank",
        "runs",
        "engine_build_id IS NULL OR btrim(engine_build_id) <> ''",
    )

    op.add_column("decisions", sa.Column("engine_build_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_decisions_engine_build_id_nonblank",
        "decisions",
        "engine_build_id IS NULL OR btrim(engine_build_id) <> ''",
    )


def downgrade() -> None:
    conn = op.get_bind()
    used = conn.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM policy_bundles) "
            "+ (SELECT count(*) FROM bundle_pinning_epoch) "
            "+ (SELECT count(*) FROM checks WHERE policy_bundle_hash IS NOT NULL) "
            "+ (SELECT count(*) FROM runs WHERE engine_build_id IS NOT NULL) "
            "+ (SELECT count(*) FROM decisions WHERE engine_build_id IS NOT NULL)"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(
            "migration 011 is forward-only after use: policy bundles / pinning "
            "provenance have been recorded on immutable rows; roll forward instead."
        )
    op.drop_column("decisions", "engine_build_id")
    op.drop_column("runs", "engine_build_id")
    op.drop_column("checks", "policy_bundle_hash")
    op.drop_table("bundle_pinning_epoch")  # epoch before policy_bundles (FK)
    op.drop_table("policy_bundles")
