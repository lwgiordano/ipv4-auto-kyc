"""Immutable console configuration revisions; installation does not activate them.

Revision ID: 024
Revises: 023

Legacy runs retain NULL configuration provenance. Platform ordering activation
is separately reserved as 025 and remains unbuilt.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "configuration_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("parent_revision", sa.BigInteger(), sa.ForeignKey("configuration_revisions.id")),
        sa.Column(
            "policy_bundle_hash", sa.Text(), sa.ForeignKey("policy_bundles.bundle_hash"), nullable=False
        ),
        sa.Column("brokers_json", JSONB(), nullable=False),
        sa.Column("mappings_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("change_kind", sa.Text(), nullable=False),
        sa.UniqueConstraint("id", "policy_bundle_hash", name="uq_configuration_revision_bundle"),
        sa.CheckConstraint("id > 0", name="ck_configuration_revision_id"),
        sa.CheckConstraint("schema_version = 1", name="ck_configuration_schema_version"),
        sa.CheckConstraint(
            "parent_revision IS NULL OR (parent_revision > 0 AND parent_revision < id)",
            name="ck_configuration_parent",
        ),
        sa.CheckConstraint("policy_bundle_hash ~ '^[0-9a-f]{64}$'", name="ck_configuration_bundle_hash"),
        sa.CheckConstraint("jsonb_typeof(brokers_json) = 'array'", name="ck_configuration_brokers_array"),
        sa.CheckConstraint("jsonb_typeof(mappings_json) = 'object'", name="ck_configuration_mappings_object"),
        sa.CheckConstraint("btrim(created_by) <> ''", name="ck_configuration_created_by"),
        sa.CheckConstraint(
            "change_kind IN ('baseline','points','brokers','mappings')", name="ck_configuration_change_kind"
        ),
        sa.CheckConstraint(
            "(change_kind = 'baseline') = (parent_revision IS NULL)", name="ck_configuration_baseline_parent"
        ),
    )
    op.create_table(
        "configuration_state",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column(
            "active_revision", sa.BigInteger(), sa.ForeignKey("configuration_revisions.id"), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_configuration_state_singleton"),
        sa.CheckConstraint("active_revision > 0", name="ck_configuration_active_revision"),
    )
    op.create_table(
        "configuration_requests",
        sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column(
            "result_revision", sa.BigInteger(), sa.ForeignKey("configuration_revisions.id"), nullable=False
        ),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("request_digest ~ '^[0-9a-f]{64}$'", name="ck_configuration_request_digest"),
        sa.CheckConstraint("result_revision > 0", name="ck_configuration_request_revision"),
    )
    op.add_column("runs", sa.Column("configuration_revision", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("matched_broker_entity_id", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("matched_identifier_class", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_runs_configuration_bundle",
        "runs",
        "configuration_revisions",
        ["configuration_revision", "policy_bundle_hash"],
        ["id", "policy_bundle_hash"],
    )
    op.create_check_constraint(
        "ck_runs_configuration_pin",
        "runs",
        "configuration_revision IS NULL OR (configuration_revision > 0 AND policy_bundle_hash IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_runs_configuration_match",
        "runs",
        "(matched_broker_entity_id IS NULL AND matched_identifier_class IS NULL) OR "
        "(configuration_revision IS NOT NULL AND matched_broker_entity_id IS NOT NULL "
        "AND btrim(matched_broker_entity_id) <> '' AND matched_identifier_class IS NOT NULL "
        "AND matched_identifier_class IN ('legal_name','domains','email_domains',"
        "'rir_org_ids','poc_handles','asns'))",
    )
    op.execute("""
        CREATE FUNCTION public.refuse_configuration_history_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'CONFIGURATION_IMMUTABLE: % refuses %', TG_TABLE_NAME, TG_OP;
        END;
        $$;
        CREATE TRIGGER configuration_revisions_immutable
        BEFORE UPDATE OR DELETE ON public.configuration_revisions
        FOR EACH ROW EXECUTE FUNCTION public.refuse_configuration_history_mutation();
        CREATE TRIGGER configuration_requests_immutable
        BEFORE UPDATE OR DELETE ON public.configuration_requests
        FOR EACH ROW EXECUTE FUNCTION public.refuse_configuration_history_mutation();
        CREATE FUNCTION public.refuse_run_configuration_repin() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.configuration_revision IS DISTINCT FROM OLD.configuration_revision
               OR (OLD.configuration_revision IS NOT NULL
                   AND NEW.policy_bundle_hash IS DISTINCT FROM OLD.policy_bundle_hash) THEN
                RAISE EXCEPTION 'CONFIGURATION_PIN_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER runs_configuration_pin_immutable BEFORE UPDATE ON public.runs
        FOR EACH ROW EXECUTE FUNCTION public.refuse_run_configuration_repin();
    """)


def downgrade() -> None:
    conn = op.get_bind()
    # Relation locks in one statement are acquired sequentially, not atomically.
    # Never wait while holding a parent lock: an INSERT can already hold the
    # child lock and be about to check its parent FK. NOWAIT fails the migration
    # transaction instead of deadlocking (and possibly aborting) that writer.
    try:
        conn.execute(
            sa.text(
                "LOCK TABLE public.configuration_state, public.configuration_revisions, "
                "public.configuration_requests, public.runs IN ACCESS EXCLUSIVE MODE NOWAIT"
            )
        )
    except sa.exc.DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise RuntimeError(
            "MIGRATION_024_CONFIGURATION_DOWNGRADE_BUSY: configuration authorities are busy; "
            "quiesce writers before retrying; recorded history still requires roll-forward recovery"
        ) from exc
    # An inactive stored baseline is still historical evidence, so refuse that too.
    used = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM public.configuration_state) "
            "OR EXISTS (SELECT 1 FROM public.configuration_revisions) "
            "OR EXISTS (SELECT 1 FROM public.configuration_requests) "
            "OR EXISTS (SELECT 1 FROM public.runs WHERE configuration_revision IS NOT NULL)"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(
            "MIGRATION_024_CONFIGURATION_DOWNGRADE_REFUSED: configuration history exists; roll forward"
        )
    op.execute("DROP TRIGGER runs_configuration_pin_immutable ON public.runs")
    op.execute("DROP FUNCTION public.refuse_run_configuration_repin()")
    op.drop_constraint("fk_runs_configuration_bundle", "runs", type_="foreignkey")
    op.drop_constraint("ck_runs_configuration_pin", "runs", type_="check")
    op.drop_constraint("ck_runs_configuration_match", "runs", type_="check")
    for column in ("matched_identifier_class", "matched_broker_entity_id", "configuration_revision"):
        op.drop_column("runs", column)
    op.drop_table("configuration_requests")
    op.drop_table("configuration_state")
    op.drop_table("configuration_revisions")
    op.execute("DROP FUNCTION public.refuse_configuration_history_mutation()")
