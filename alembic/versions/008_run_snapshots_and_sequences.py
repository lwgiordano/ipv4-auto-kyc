"""run input snapshots + per-case event sequence

Revision ID: 008
Revises: 007

Freezes each run's inputs and gives every event a gap-free per-case sequence.

- runs.input_snapshot_json is NULLABLE on purpose: historical runs never
  recorded a snapshot, and backfilling one from today's cases.submitted_json
  would attribute inputs a past run never saw — fabricated provenance in a
  compliance audit trail. NULL means "pre-migration, unrecorded"; the NOT-NULL
  invariant is enforced in code for new runs.
- events.event_sequence is backfilled from arrival order (received_at, id). That
  is a deterministic RECONSTRUCTION, not proof of the original order (received_at
  ties are ambiguous), so backfilled rows are flagged sequence_backfilled=true.
- cases.event_sequence is the per-case counter (each case's max sequence).
- UNIQUE(case_id, event_sequence) is the backstop for the in-code allocator.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("input_snapshot_json", postgresql.JSONB, nullable=True))
    op.add_column("events", sa.Column("event_sequence", sa.BigInteger(), nullable=True))
    op.add_column(
        "events",
        sa.Column(
            "sequence_backfilled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "cases",
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # Reconstruct event ordinals from arrival order and flag them as backfilled.
    op.execute(
        """
        WITH ordered AS (
            SELECT id, row_number() OVER (
                PARTITION BY case_id ORDER BY received_at, id
            ) AS seq
            FROM events
        )
        UPDATE events e
        SET event_sequence = o.seq, sequence_backfilled = true
        FROM ordered o
        WHERE e.id = o.id
        """
    )
    # Per-case counter = each case's maximum reconstructed sequence.
    op.execute(
        """
        UPDATE cases c
        SET event_sequence = COALESCE(
            (SELECT max(event_sequence) FROM events e WHERE e.case_id = c.id), 0
        )
        """
    )

    op.alter_column("events", "event_sequence", nullable=False)
    op.create_unique_constraint(
        "uq_events_case_event_sequence", "events", ["case_id", "event_sequence"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_events_case_event_sequence", "events", type_="unique")
    op.drop_column("cases", "event_sequence")
    op.drop_column("events", "sequence_backfilled")
    op.drop_column("events", "event_sequence")
    op.drop_column("runs", "input_snapshot_json")
