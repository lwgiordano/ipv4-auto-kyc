"""hmac v2 per-case idempotency + v1 observation witness

Revision ID: 010
Revises: 009

Re-scopes event idempotency from global to (case_id, idempotency_key) [D3] and
adds the durable, fail-closed v1 acceptance witness + diagnostic counters.

NON-HOT: the old image's ingest does ON CONFLICT (idempotency_key), which needs
the global unique this migration drops — an old replica still serving after the
migration would fail event inserts. Deploy stop/migrate/start (see docs/RUNBOOK).

Downgrade is FORWARD-ONLY after cross-case reuse: once (case-a,key) and
(case-b,key) coexist, the old global unique cannot be recreated, so downgrade
refuses loudly rather than deleting immutable audit events.
"""

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_events_idempotency_key", "events", type_="unique")
    op.create_unique_constraint("uq_events_case_idempotency", "events", ["case_id", "idempotency_key"])

    op.create_table(
        "hmac_v1_observation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("observation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Seed INACTIVE — observation_started_at NULL. The clock starts ONLY at the
    # post-cutover activation command, never here, so a straggler old replica
    # accepting un-witnessed v1 cannot turn the zero predicate green prematurely.
    op.execute("INSERT INTO hmac_v1_observation (id, accepted_count) VALUES (1, 0)")

    op.create_table(
        "hmac_signature_stats",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    dupes = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM (SELECT idempotency_key FROM events "
                "GROUP BY idempotency_key HAVING count(DISTINCT case_id) > 1) d"
            )
        )
        .scalar_one()
    )
    if dupes:
        raise RuntimeError(
            f"cross-case idempotency reuse present ({dupes} key(s)); migration 010 is "
            "forward-only — refusing downgrade (recreating the global unique would "
            "require deleting immutable audit events)"
        )
    op.drop_table("hmac_signature_stats")
    op.drop_table("hmac_v1_observation")
    op.drop_constraint("uq_events_case_idempotency", "events", type_="unique")
    op.create_unique_constraint("uq_events_idempotency_key", "events", ["idempotency_key"])
