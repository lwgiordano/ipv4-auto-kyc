"""outbox stream separation + local decision ordering (PR 7b-core)

Revision ID: 013
Revises: 012

Part A (this file, built across PR 7b-core tasks): adds ordering_stream (real
NOT NULL), the fenced-claim columns, case_id NOT NULL + FK, decisions.decision_sequence,
cases.last_decision_sequence, the closed kind/stream vocab CHECKs, and the exhaustive
per-status lifecycle + claim-tuple CHECK. The legacy decision_sequence backfill
(Task 2), the decision-identity constraints (Task 4), and the race-safe downgrade
(Task 6) are added at the marked insertion points.

Drained cutover: writers are stopped, so the plain SET NOT NULL scan is safe and no
CONCURRENTLY is needed. A DB default for ordering_stream is deliberately avoided — it
would mislabel emails; the value is derived from kind in two explicit branches.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

# superseded is a DECISION-ONLY terminal (re-audit F8): production supersedes only decision
# callbacks; A6's zero-send exception and the ordering proof are callback-specific, so a
# poc_email must never be able to enter 'superseded' (not even by operator UPDATE).
_LIFECYCLE_CHECK = """
    status IN ('pending','delivered','dead','superseded')
    AND (status <> 'pending' OR (
          delivered_at IS NULL AND resolved_at IS NULL AND (
            (claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL)
            OR (claim_token IS NOT NULL AND claim_lease_expires_at IS NOT NULL AND claimed_by IS NOT NULL))))
    AND (status <> 'delivered' OR (
          delivered_at IS NOT NULL AND resolved_at IS NULL
          AND claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL))
    AND (status <> 'dead' OR (
          delivered_at IS NULL AND resolved_at IS NULL
          AND claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL))
    AND (status <> 'superseded' OR (
          delivered_at IS NULL AND resolved_at IS NOT NULL
          AND claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL))
    AND (status <> 'superseded' OR kind = 'decision_callback')
"""


def upgrade() -> None:
    conn = op.get_bind()

    # --- shared parity preflight (fail-closed BEFORE any DDL) ---
    from kyc_tool.migration_contracts.v013_backfill import BLOCKED_SENTINEL, MISSING_CALLBACK, run_parity

    violations = run_parity(conn)
    if violations:
        names = {n for n, _ in violations}
        detail = "; ".join(f"{n}: {ids}" for n, ids in violations)
        if MISSING_CALLBACK in names:
            raise RuntimeError(
                f"migration 013: {BLOCKED_SENTINEL} — automatic decision(s) without a surviving "
                f"decision_callback; restore from authoritative backup or remain on 012. {detail}"
            )
        raise RuntimeError(f"migration 013: backfill parity violation(s), fail-closed: {detail}")

    # --- columns (all additive/nullable first) ---
    op.add_column("outbox", sa.Column("ordering_stream", sa.Text(), nullable=True))
    op.add_column("outbox", sa.Column("decision_sequence", sa.BigInteger(), nullable=True))
    op.add_column("outbox", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox", sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox", sa.Column("claim_token", postgresql.UUID(), nullable=True))
    op.add_column("outbox", sa.Column("claimed_by", sa.Text(), nullable=True))
    # The recorded digest of the bytes actually sent, written in the delivery transaction. NOT
    # backfilled: payload_json is jsonb, which normalizes key order, so a historical row's sent
    # bytes are unrecoverable and a computed digest would fabricate a witness. Pre-013 deliveries
    # are un-witnessed by construction (spec rev 12).
    op.add_column("outbox", sa.Column("callback_wire_sha256", sa.Text(), nullable=True))
    op.add_column("outbox", sa.Column("wire_version", sa.Text(), nullable=True))
    op.add_column("decisions", sa.Column("decision_sequence", sa.BigInteger(), nullable=True))
    op.add_column(
        "cases",
        sa.Column("last_decision_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # --- ordering_stream backfill: two explicit branches, no DB default ---
    op.execute(
        """
        UPDATE outbox SET ordering_stream = CASE
            WHEN kind='poc_email' THEN 'email'
            WHEN kind='decision_callback' THEN 'decision'
            ELSE NULL END
        """
    )
    unmapped = conn.execute(
        sa.text("SELECT id, kind FROM outbox WHERE ordering_stream IS NULL")
    ).fetchall()
    if unmapped:
        raise RuntimeError(
            "migration 013: outbox rows with unmappable kind (cannot assign ordering_stream): "
            f"{[(r.id, r.kind) for r in unmapped]}"
        )

    # --- case_id NOT NULL + FK (both enqueue APIs require a case; removes the claim bypass) ---
    orphan_case = conn.execute(
        sa.text("SELECT id FROM outbox WHERE case_id IS NULL OR case_id NOT IN (SELECT id FROM cases)")
    ).fetchall()
    if orphan_case:
        raise RuntimeError(
            f"migration 013: outbox rows with NULL/orphan case_id: {[r.id for r in orphan_case]}"
        )
    op.alter_column("outbox", "case_id", existing_type=sa.Text(), nullable=False)
    op.create_foreign_key("fk_outbox_case_id", "outbox", "cases", ["case_id"], ["id"])

    # --- ordering_stream real SET NOT NULL + permanent vocabulary CHECK ---
    op.alter_column("outbox", "ordering_stream", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_outbox_ordering_stream_vocab", "outbox", "ordering_stream IN ('decision','email')"
    )

    # --- closed kind vocabulary CHECK (an unknown kind otherwise reaches _deliver's raise) ---
    op.create_check_constraint(
        "ck_outbox_kind_vocab", "outbox", "kind IN ('decision_callback','poc_email')"
    )

    # --- exhaustive per-status lifecycle + claim-tuple CHECK ---
    op.create_check_constraint("ck_outbox_status_lifecycle", "outbox", _LIFECYCLE_CHECK)
    # digest shape, paired presence, and decision-callback-only — a poc_email never has sent
    # callback bytes, so it must never carry a wire witness.
    op.create_check_constraint(
        "ck_outbox_wire_witness", "outbox",
        "(callback_wire_sha256 IS NULL) = (wire_version IS NULL) "
        "AND (callback_wire_sha256 IS NULL OR callback_wire_sha256 ~ '^[0-9a-f]{64}$') "
        "AND (wire_version IS NULL OR wire_version IN ('legacy','sequenced')) "
        "AND (callback_wire_sha256 IS NULL OR kind = 'decision_callback')",
    )

    # --- legacy decision_sequence assignment; ORDER AUTHORITY = outbox.id (rev-4 F1) ---
    # decided_at is txn-start now() and _decide_txn opens its txn before the case FOR UPDATE,
    # so two same-case decides can invert decided_at vs their lock-serialized commit order.
    # outbox.id is allocated at enqueue UNDER that lock, so it preserves the true serialization.
    op.execute(
        """
        WITH seq AS (
            SELECT d.id AS decision_id,
                   row_number() OVER (PARTITION BY d.case_id ORDER BY o.id) AS s
            FROM decisions d
            JOIN outbox o ON o.kind='decision_callback' AND o.run_id = d.run_id
            WHERE d.manual = false
        )
        UPDATE decisions d SET decision_sequence = seq.s FROM seq WHERE d.id = seq.decision_id
        """
    )
    op.execute(
        """
        UPDATE outbox o SET decision_sequence = d.decision_sequence
        FROM decisions d
        WHERE o.kind='decision_callback' AND o.run_id = d.run_id AND d.manual = false
        """
    )
    op.execute(
        """
        UPDATE cases c SET last_decision_sequence = COALESCE(
            (SELECT max(d.decision_sequence) FROM decisions d
             WHERE d.case_id = c.id AND d.decision_sequence IS NOT NULL), 0)
        """
    )
    post_dup = conn.execute(
        sa.text(
            "SELECT case_id, decision_sequence FROM decisions WHERE decision_sequence IS NOT NULL "
            "GROUP BY case_id, decision_sequence HAVING count(*) > 1"
        )
    ).fetchall()
    if post_dup:  # defensive: row_number() cannot produce a per-case duplicate
        raise RuntimeError(f"migration 013: post-backfill per-case sequence collision: {post_dup}")

    # --- runs relational identity target + composite decisions→runs FK (re-audit F2) ---
    # The single-column FKs bind decisions.case_id and decisions.run_id INDEPENDENTLY, so a
    # post-migration INSERT/UPDATE could still pair run r1 (case c1) with case c2 — the exact
    # tuple the parity preflight classifies as decision_case_ne_run_case. The named unique
    # target MUST exist before the composite FK that references it.
    op.create_unique_constraint("uq_runs_id_case_id", "runs", ["id", "case_id"])
    # run_id is nullable → MATCH SIMPLE semantics: a manual decision (run_id NULL) is exempt
    # by design, while every automatic decision (run_id NOT NULL) is fully bound to its run's
    # OWN case. This is the intended semantics, not an accident.
    op.create_foreign_key(
        "fk_decisions_run_case", "decisions", "runs", ["run_id", "case_id"], ["id", "case_id"]
    )
    # --- decisions identity + per-case ordering (F1) ---
    # UNIQUE(run_id): one automatic decision per run (multiple NULL manual rows stay legal).
    op.create_unique_constraint("uq_decisions_run_id", "decisions", ["run_id"])
    # THE per-case ordering invariant (manual rows keep decision_sequence NULL → many NULLs legal).
    op.create_unique_constraint(
        "uq_decisions_case_decision_sequence", "decisions", ["case_id", "decision_sequence"]
    )
    # The exact triple-FK target for the callback binding below.
    op.create_unique_constraint(
        "uq_decisions_run_case_sequence", "decisions", ["run_id", "case_id", "decision_sequence"]
    )
    # NULL-explicit exhaustive row shapes (F2). An implication form like
    # `NOT(manual=false) OR (run_id IS NOT NULL AND decision_sequence>0)` evaluates to UNKNOWN
    # (and thus PASSES) for (manual=false, run_id set, decision_sequence NULL) — a hole. Spell
    # every column's NULL-ness in each branch so a NULL sequence on an automatic row is rejected.
    op.create_check_constraint(
        "ck_decisions_manual_sequence", "decisions",
        "((manual = false AND run_id IS NOT NULL AND decision_sequence IS NOT NULL "
        "AND decision_sequence > 0) "
        "OR (manual = true AND run_id IS NULL AND decision_sequence IS NULL))",
    )
    # --- outbox callback binding: NULL-explicit exhaustive shape + triple FK + one callback per run ---
    op.create_check_constraint(
        "ck_outbox_kind_stream_identity", "outbox",
        "((kind='decision_callback' AND ordering_stream='decision' AND run_id IS NOT NULL "
        "AND decision_sequence IS NOT NULL AND decision_sequence > 0) "
        "OR (kind='poc_email' AND ordering_stream='email' AND run_id IS NULL "
        "AND decision_sequence IS NULL))",
    )
    op.create_foreign_key(
        "fk_outbox_decision_triple", "outbox", "decisions",
        ["run_id", "case_id", "decision_sequence"], ["run_id", "case_id", "decision_sequence"],
    )
    op.create_index(
        "uq_outbox_decision_callback_run", "outbox", ["run_id"], unique=True,
        postgresql_where=sa.text("kind='decision_callback'"),
    )

    # --- stream claim index ---
    # Partial to the CLAIMABLE set. 013 makes decision_callback rows non-prunable (the durable
    # ordering authority), so delivered/superseded terminals accumulate without bound; a full index
    # would grow forever and drag that dead weight into every claim plan. `status` leaves the key
    # because the predicate pins it.
    op.create_index(
        "ix_outbox_stream_claim", "outbox",
        ["case_id", "ordering_stream", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Same reasoning for the legacy 006 claim index, which is still full.
    op.drop_index("ix_outbox_claim", table_name="outbox")
    op.create_index(
        "ix_outbox_claim", "outbox", ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    # Reversible only BEFORE the first local supersession. Lock FIRST: a bare
    # EXISTS preflight is a TOCTOU race (ACCESS SHARE is compatible with a publisher's
    # ROW EXCLUSIVE, so a superseded row could commit between the check and the DDL,
    # stranding an unknown, unprunable terminal a pre-7b image cannot interpret).
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    stranded = op.get_bind().execute(
        sa.text("SELECT count(*) FROM outbox WHERE status='superseded'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            "migration 013 downgrade refused: superseded outbox row(s) exist — a pre-7b "
            "image cannot interpret or prune them (roll forward instead)"
        )
    # A wire witness is immutable delivery evidence — the digest of bytes the platform actually
    # accepted. Dropping the column would destroy it, and "the row is terminal locally" is not a
    # reason: the digest is exactly what a later reconciliation compares against the platform's
    # ledger. Forward-only once any witness exists (re-audit 1f8412e F3).
    witnessed = op.get_bind().execute(
        sa.text("SELECT count(*) FROM outbox WHERE callback_wire_sha256 IS NOT NULL")
    ).scalar_one()
    if witnessed:
        raise RuntimeError(
            "MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE: delivery witness(es) exist — "
            "dropping callback_wire_sha256 would destroy immutable delivery evidence. Roll back "
            "by flag/image on the compatible schema instead (RUNBOOK: rollback after witness use)."
        )
    # A database stamped '013' by the briefly-published AMENDED revision carries
    # outbox_delivery_attempts, which this downgrade does not manage. Refuse rather than strand
    # an orphan evidence table on a 012 schema; repair revision 014 governs that table.
    amended = op.get_bind().execute(
        sa.text("SELECT to_regclass('outbox_delivery_attempts') IS NOT NULL")
    ).scalar_one()
    if amended:
        raise RuntimeError(
            "MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY: outbox_delivery_attempts exists "
            "but this database never applied repair revision 014 — upgrade to 014 first so the "
            "witness-use guard governs the attempt authority, then downgrade through it."
        )
    op.drop_index("ix_outbox_stream_claim", table_name="outbox")
    op.drop_index("ix_outbox_claim", table_name="outbox")   # restore 006's full form
    op.create_index("ix_outbox_claim", "outbox", ["status", "next_attempt_at"])
    # decision-identity constraints (Task 4), reverse dependency order
    op.drop_index("uq_outbox_decision_callback_run", table_name="outbox")
    op.drop_constraint("fk_outbox_decision_triple", "outbox", type_="foreignkey")
    op.drop_constraint("ck_outbox_kind_stream_identity", "outbox", type_="check")
    op.drop_constraint("ck_decisions_manual_sequence", "decisions", type_="check")
    op.drop_constraint("uq_decisions_run_case_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_case_decision_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_run_id", "decisions", type_="unique")
    # composite decisions→runs case FK (re-audit F2): FK before its unique target
    op.drop_constraint("fk_decisions_run_case", "decisions", type_="foreignkey")
    op.drop_constraint("uq_runs_id_case_id", "runs", type_="unique")
    # outbox stream / case_id / lifecycle (Task 1)
    op.drop_constraint("ck_outbox_wire_witness", "outbox", type_="check")
    op.drop_constraint("ck_outbox_status_lifecycle", "outbox", type_="check")
    op.drop_constraint("ck_outbox_kind_vocab", "outbox", type_="check")
    op.drop_constraint("ck_outbox_ordering_stream_vocab", "outbox", type_="check")
    op.drop_constraint("fk_outbox_case_id", "outbox", type_="foreignkey")
    op.alter_column("outbox", "case_id", existing_type=sa.Text(), nullable=True)
    op.drop_column("cases", "last_decision_sequence")
    op.drop_column("decisions", "decision_sequence")
    op.drop_column("outbox", "wire_version")
    op.drop_column("outbox", "callback_wire_sha256")
    op.drop_column("outbox", "claimed_by")
    op.drop_column("outbox", "claim_token")
    op.drop_column("outbox", "claim_lease_expires_at")
    op.drop_column("outbox", "resolved_at")
    op.drop_column("outbox", "decision_sequence")
    op.drop_column("outbox", "ordering_stream")
