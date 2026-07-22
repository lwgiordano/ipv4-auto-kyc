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
a rolling deploy. The three provenance-column CHECKs on the existing checks/
runs/decisions tables are added NOT VALID here — a brief, metadata-only lock
with no scan of pre-existing rows — and validated in a separate follow-on
revision (012). That two-step is what keeps this migration hot-compatible on
production-sized audit tables: a single-step validating ADD CONSTRAINT would
hold ACCESS EXCLUSIVE for the full table scan, stalling every concurrent
ingest/decide writer until it commits.

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

    # NOT VALID: metadata-only, no scan of pre-existing rows, brief lock. Still
    # enforced against every new/updated row immediately. Validated for
    # pre-existing rows by the separate follow-on revision 012 (see module
    # docstring) under a non-blocking lock instead of here under ACCESS
    # EXCLUSIVE, which is what keeps this migration hot-compatible.
    op.add_column("checks", sa.Column("policy_bundle_hash", sa.Text(), nullable=True))
    op.execute(
        "ALTER TABLE checks ADD CONSTRAINT ck_checks_policy_bundle_hash_nonblank "
        "CHECK (policy_bundle_hash IS NULL OR btrim(policy_bundle_hash) <> '') NOT VALID"
    )

    op.add_column("runs", sa.Column("engine_build_id", sa.Text(), nullable=True))
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_engine_build_id_nonblank "
        "CHECK (engine_build_id IS NULL OR btrim(engine_build_id) <> '') NOT VALID"
    )

    op.add_column("decisions", sa.Column("engine_build_id", sa.Text(), nullable=True))
    op.execute(
        "ALTER TABLE decisions ADD CONSTRAINT ck_decisions_engine_build_id_nonblank "
        "CHECK (engine_build_id IS NULL OR btrim(engine_build_id) <> '') NOT VALID"
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
