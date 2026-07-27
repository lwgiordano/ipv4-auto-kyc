"""outbox witness repair: attempt authority, generation marker, read pointer (PR 7b-core)

Revision ID: 014
Revises: 013

This revision exists because `013` was amended in place after it had been committed
(`9092fdb` → `50293ba`): the attempt-authority table was added to an already-published
revision, so any database stamped `013` by the original file would never receive it —
Alembic performs no work for a recorded revision, and fresh-database CI structurally
cannot detect the split. `013` is restored to its committed shape and frozen by hash;
everything the amend added, plus the re-audit (`1f8412e`) repairs, lands here instead.

Content:
- `outbox_delivery_attempts` — created when absent; when present (a database that ran
  the briefly-published amended `013`), validated column-by-column and fail-closed on
  any mismatch, because a half-right evidence table is worse than none.
- an immutability trigger on attempts: UPDATE is always refused; DELETE is refused
  unless the parent row carries a terminal digest (then the attempt is redundant).
- `outbox.witness_generation` — 'legacy' rows predate the attempt authority, so the
  absence of an attempt proves nothing about them; only an 'attempt_v1' row with no
  attempt may be called `not_accepted`. Backfilled conservatively: anything without
  an attempt is 'legacy'.
- `ck_outbox_wire_witness_delivered` — a terminal digest is only writable on a
  delivered row, so a raw digest on a pending row cannot masquerade as
  `delivery_witnessed` and license attempt deletion.
- `cases.latest_decision_row_id` — the atomic latest-decision authority, maintained by
  a trigger on decisions INSERT (same transaction as the insert, both decide paths,
  no application ordering to forget), composite-FK-bound to the same case.
  `decided_at` is transaction-start time and can invert against lock-serialized commit
  order — `013` records this for the backfill; the read API was still sorting by it.
- `ix_outbox_live_status` — partial index matching the exact live-status predicate
  (`pending`,`dead`); `013`'s claim indexes are pending-only and cannot serve it, so
  the "bounded" metrics query could seq-scan the ever-growing terminal history.

Downgrade is forward-only once ANY witness exists (attempt row or terminal digest):
witnesses are immutable delivery evidence, and local terminal status is not a reason
to destroy the record of what the platform accepted.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH"
_REFUSED_SENTINEL = "MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE"

# What the attempt authority must look like, exactly. Used both to create it and to
# validate a pre-existing copy left by the amended `013` — one definition, so the two
# paths cannot drift.
_EXPECTED_COLUMNS = [
    # (name, data_type, is_nullable)
    ("attempt_id", "uuid", "NO"),
    ("outbox_id", "bigint", "NO"),
    ("claim_token", "uuid", "NO"),
    ("wire_version", "text", "NO"),
    ("request_sha256", "text", "NO"),
    ("attempted_at", "timestamp with time zone", "NO"),
]
_EXPECTED_CONSTRAINTS = {
    "ck_attempt_sha_shape": "request_sha256 ~ '^[0-9a-f]{64}$'",
    "ck_attempt_wire_vocab": "wire_version",  # vocabulary membership; def form varies
    "fk_attempt_outbox": "ON DELETE CASCADE",
}


def _create_attempt_authority() -> None:
    op.create_table(
        "outbox_delivery_attempts",
        sa.Column("attempt_id", postgresql.UUID(), primary_key=True),
        sa.Column("outbox_id", sa.BigInteger(), nullable=False),
        # the claim the attempt was made under: proves a stale claimant wrote nothing,
        # and lets a reconciler tell "one claimant retried" from "two claimants both sent".
        sa.Column("claim_token", postgresql.UUID(), nullable=False),
        sa.Column("wire_version", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["outbox_id"], ["outbox.id"], name="fk_attempt_outbox",
                                ondelete="CASCADE"),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_attempt_sha_shape"),
        sa.CheckConstraint("wire_version IN ('legacy','sequenced')", name="ck_attempt_wire_vocab"),
    )
    op.create_index(
        "ix_attempt_outbox", "outbox_delivery_attempts",
        ["outbox_id", sa.text("attempted_at DESC")],
    )


def _validate_attempt_authority(conn) -> None:
    """A database that ran the amended `013` already has the table. Accept it only if it
    is EXACTLY right; a partial or drifted copy fails closed, because evidence written
    into a mismatched table is evidence that cannot be trusted later."""
    problems: list[str] = []

    cols = conn.execute(sa.text(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'outbox_delivery_attempts' ORDER BY ordinal_position"
    )).fetchall()
    actual = [(c.column_name, c.data_type, c.is_nullable) for c in cols]
    if actual != _EXPECTED_COLUMNS:
        problems.append(f"columns: expected {_EXPECTED_COLUMNS}, found {actual}")

    default = conn.execute(sa.text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name='outbox_delivery_attempts' AND column_name='attempted_at'"
    )).scalar()
    if default != "now()":
        problems.append(f"attempted_at default: expected now(), found {default!r}")

    pk = conn.execute(sa.text(
        "SELECT a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.conrelid = 'outbox_delivery_attempts'::regclass AND c.contype = 'p'"
    )).scalars().all()
    if pk != ["attempt_id"]:
        problems.append(f"primary key: expected ['attempt_id'], found {pk}")

    condefs = {
        r.conname: r.condef
        for r in conn.execute(sa.text(
            "SELECT conname, pg_get_constraintdef(oid) AS condef FROM pg_constraint "
            "WHERE conrelid = 'outbox_delivery_attempts'::regclass"
        ))
    }
    for name, must_contain in _EXPECTED_CONSTRAINTS.items():
        if name not in condefs:
            problems.append(f"constraint {name}: MISSING")
        elif must_contain not in condefs[name]:
            problems.append(f"constraint {name}: {condefs[name]!r} lacks {must_contain!r}")
    fk = condefs.get("fk_attempt_outbox", "")
    if fk and "REFERENCES outbox(id)" not in fk:
        problems.append(f"fk_attempt_outbox: {fk!r} does not reference outbox(id)")

    indexdef = conn.execute(sa.text(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename='outbox_delivery_attempts' AND indexname='ix_attempt_outbox'"
    )).scalar()
    if indexdef is None:
        problems.append("index ix_attempt_outbox: MISSING")
    elif "(outbox_id, attempted_at DESC)" not in indexdef:
        problems.append(f"index ix_attempt_outbox: unexpected definition {indexdef!r}")

    if problems:
        raise RuntimeError(
            f"migration 014: {_MISMATCH_SENTINEL} — outbox_delivery_attempts exists (amended-013 "
            f"history) but does not match the canonical shape; refusing to adopt it. "
            f"Repair or drop it under operator control first. Mismatches: {problems}"
        )


def upgrade() -> None:
    conn = op.get_bind()

    # --- attempt authority: create when absent, validate exhaustively when present ---
    exists = conn.execute(
        sa.text("SELECT to_regclass('outbox_delivery_attempts') IS NOT NULL")
    ).scalar_one()
    if exists:
        _validate_attempt_authority(conn)
    else:
        _create_attempt_authority()

    # --- immutability: attempts are evidence, not state ---
    # UPDATE is never legal. DELETE is legal only when the parent row already carries a
    # terminal digest — then the attempt is redundant and retention may reclaim it. For any
    # other row the attempt is the only proof bytes were staged for the wire, and deleting
    # it would let the witness taxonomy assert "nothing was ever transmitted".
    op.execute(
        """
        CREATE FUNCTION outbox_attempts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'outbox_delivery_attempts is insert-only (attempt % )', OLD.attempt_id;
            END IF;
            IF EXISTS (SELECT 1 FROM outbox o
                       WHERE o.id = OLD.outbox_id AND o.callback_wire_sha256 IS NOT NULL) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'attempt % is the sole delivery evidence for outbox row %; deletion refused',
                OLD.attempt_id, OLD.outbox_id;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_attempts_immutable "
        "BEFORE UPDATE OR DELETE ON outbox_delivery_attempts "
        "FOR EACH ROW EXECUTE FUNCTION outbox_attempts_guard()"
    )

    # --- witness generation marker (re-audit F2) ---
    # 'legacy' rows predate the attempt authority: the publisher then sent HTTP before any
    # durable record, so a legacy pending/dead row may well have reached the platform, and
    # the absence of an attempt proves NOTHING about it. Only an 'attempt_v1' row — created
    # under the record-intent-first regime — with no attempt may be classified not_accepted.
    # DEFAULT 'legacy' is the fail-closed direction: a write path that forgets to stamp the
    # generation degrades to over-caution (legacy_unwitnessed), never to false proof.
    op.add_column(
        "outbox",
        sa.Column("witness_generation", sa.Text(), nullable=False, server_default="legacy"),
    )
    op.create_check_constraint(
        "ck_outbox_witness_generation", "outbox", "witness_generation IN ('legacy','attempt_v1')"
    )
    op.execute(
        "UPDATE outbox SET witness_generation = 'attempt_v1' WHERE EXISTS "
        "(SELECT 1 FROM outbox_delivery_attempts a WHERE a.outbox_id = outbox.id)"
    )

    # --- a terminal digest belongs to a delivered row, and nowhere else (re-audit F6) ---
    # Without this, a raw digest UPDATE on a pending row reads as delivery_witnessed and
    # licenses the deletion of its real attempts.
    op.create_check_constraint(
        "ck_outbox_wire_witness_delivered", "outbox",
        "callback_wire_sha256 IS NULL OR status = 'delivered'",
    )

    # --- atomic latest-decision authority (re-audit F4) ---
    # now() is transaction-start time, so decided_at can invert against the lock-serialized
    # commit order (013's backfill orders by outbox.id for exactly this reason). The read
    # API must therefore never pick "latest" by decided_at. The pointer is set by a trigger
    # on decisions INSERT: same transaction, both decide paths (automatic + manual), and no
    # application call-site ordering to forget. All decision inserts happen under the case
    # row lock, so pointer updates serialize with the projection they accompany.
    op.create_unique_constraint("uq_decisions_id_case_id", "decisions", ["id", "case_id"])
    op.add_column("cases", sa.Column("latest_decision_row_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_cases_latest_decision", "cases", "decisions",
        ["latest_decision_row_id", "id"], ["id", "case_id"],
    )
    # Backfill from unambiguous serialization authority only. decision_sequence is allocated
    # under the case lock, so for all-automatic cases max(sequence) IS the latest. A case
    # with a manual decision among several has no durable record of write order — decided_at
    # would be a guess, and this migration does not guess: the pointer stays NULL (the read
    # path returns no gates rather than possibly-wrong gates) and heals on the next decide.
    op.execute(
        """
        UPDATE cases c SET latest_decision_row_id = d.id
        FROM (SELECT case_id, min(id) AS id FROM decisions GROUP BY case_id HAVING count(*) = 1) d
        WHERE c.id = d.case_id
        """
    )
    op.execute(
        """
        UPDATE cases c SET latest_decision_row_id = d.id
        FROM (
            SELECT DISTINCT ON (case_id) case_id, id
            FROM decisions
            WHERE case_id IN (SELECT case_id FROM decisions GROUP BY case_id HAVING count(*) > 1)
              AND case_id NOT IN (SELECT case_id FROM decisions WHERE manual = true)
            ORDER BY case_id, decision_sequence DESC
        ) d
        WHERE c.id = d.case_id AND c.latest_decision_row_id IS NULL
        """
    )
    ambiguous = conn.execute(sa.text(
        "SELECT c.id FROM cases c WHERE c.latest_decision_row_id IS NULL "
        "AND EXISTS (SELECT 1 FROM decisions d WHERE d.case_id = c.id)"
    )).scalars().all()
    if ambiguous:
        print(  # noqa: T201 — alembic migration output IS the operator channel
            f"migration 014: {len(ambiguous)} case(s) with ambiguous legacy decision order "
            f"(manual among several) left with a NULL latest-decision pointer; the read API "
            f"returns no gates for them until their next decision: {ambiguous}"
        )
    op.execute(
        """
        CREATE FUNCTION kyc_set_latest_decision_row() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE cases SET latest_decision_row_id = NEW.id WHERE id = NEW.case_id;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_decisions_latest_pointer AFTER INSERT ON decisions "
        "FOR EACH ROW EXECUTE FUNCTION kyc_set_latest_decision_row()"
    )

    # --- live-status index matching the exact alerting predicate (re-audit F7) ---
    # 013's claim indexes are partial to status='pending' alone; Postgres cannot use a
    # pending-only partial index for `status IN ('pending','dead')`, so the exact live
    # query could seq-scan terminal history that grows for the life of the system.
    op.create_index(
        "ix_outbox_live_status", "outbox", ["status"],
        postgresql_where=sa.text("status IN ('pending','dead')"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    # Lock BOTH tables before the witness preflight: without the attempts lock, a publisher
    # could commit an attempt between the count and the DROP, destroying evidence the count
    # never saw (same TOCTOU class as 013's superseded preflight).
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox_delivery_attempts IN ACCESS EXCLUSIVE MODE")
    attempts = conn.execute(
        sa.text("SELECT count(*) FROM outbox_delivery_attempts")
    ).scalar_one()
    digests = conn.execute(
        sa.text("SELECT count(*) FROM outbox WHERE callback_wire_sha256 IS NOT NULL")
    ).scalar_one()
    if attempts or digests:
        raise RuntimeError(
            f"migration 014 downgrade refused: {_REFUSED_SENTINEL} — {attempts} attempt row(s) "
            f"and {digests} terminal digest(s) exist. These are immutable delivery evidence; a "
            f"local terminal status is not a reason to destroy the record of what the platform "
            f"accepted. Roll back by flag/image on the compatible schema instead."
        )
    op.drop_index("ix_outbox_live_status", table_name="outbox")
    op.execute("DROP TRIGGER trg_decisions_latest_pointer ON decisions")
    op.execute("DROP FUNCTION kyc_set_latest_decision_row()")
    op.drop_constraint("fk_cases_latest_decision", "cases", type_="foreignkey")
    op.drop_column("cases", "latest_decision_row_id")
    op.drop_constraint("uq_decisions_id_case_id", "decisions", type_="unique")
    op.drop_constraint("ck_outbox_wire_witness_delivered", "outbox", type_="check")
    op.drop_constraint("ck_outbox_witness_generation", "outbox", type_="check")
    op.drop_column("outbox", "witness_generation")
    op.execute("DROP TRIGGER trg_outbox_attempts_immutable ON outbox_delivery_attempts")
    op.execute("DROP FUNCTION outbox_attempts_guard()")
    op.drop_index("ix_attempt_outbox", table_name="outbox_delivery_attempts")
    op.drop_table("outbox_delivery_attempts")
