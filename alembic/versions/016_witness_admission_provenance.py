"""witness admission provenance + canonical authority recreation (PR 7b-core)

Revision ID: 016
Revises: 015

Third repair, same rule as the first two: a published revision is never edited, so `015`'s gaps
close here. What `4dfdf8a..172fd71`'s audit proved still fabricable:

- a decision callback could go `pending → delivered` with NO witness at all — `015`'s guard fired
  only when a digest was being WRITTEN, so raw SQL writing none passed, and the taxonomy then
  called the delivered row `not_accepted`;
- attempts inserted BEFORE the admission trigger existed (under `014`, or the amended `013`) were
  trusted as `send_intent_witnessed` on mere existence — unadmitted data silently promoted into
  authority evidence by the upgrade;
- the authority functions were validated by NAME: a no-op body behind `outbox_attempts_guard()`
  passed validation and made "insert-only" decorative;
- an `attempt_v1` row with no attempt and no digest is durable NEGATIVE evidence ("nothing was
  ever staged" — the only state licensing a non-delivery claim), and the downgrades counted only
  attempts/digests, so that evidence could be destroyed by a permitted downgrade.

This revision:
1. adds `outbox_delivery_attempts.admission` (`legacy_unverified` | `admission_v1`), fast-default
   `legacy_unverified` for every pre-authority row — no row UPDATE, so `014`'s insert-only trigger
   is not violated. The admission trigger STAMPS `admission_v1` on every new insert, overwriting
   whatever the caller passed: admission is decided by the trigger, never by the writer. The
   witness taxonomy trusts only admitted attempts; an unverified attempt classifies the row
   `legacy_unwitnessed` (unproven either way), never `send_intent_witnessed` and never
   `not_accepted`.
2. DROPS and RECREATES every authority trigger/function canonically — validation by name proved
   worthless, and for objects this migration owns outright, recreation is strictly stronger than
   comparison: whatever body a mutilation left behind is erased, transactionally, with no
   operator loop. (Table shape — data-bearing — is still validated and REFUSED on mismatch.)
3. strengthens the witness guard: every FUTURE decision-callback `pending → delivered` transition
   requires a non-NULL digest/version and its matching live-claim attempt. Transition-scoped, so
   already-delivered legacy NULL rows are untouched and POC emails are unaffected.
4. downgrade refuses — child-first locks, stable sentinel — when ANY witness evidence exists,
   now including the negative kind: any `attempt_v1` decision callback, any attempt, any digest.
"""

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH"
_REFUSED_SENTINEL = "MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE"

_CANONICAL_CONSTRAINTS = {
    "outbox_delivery_attempts_pkey": "PRIMARY KEY (attempt_id)",
    "fk_attempt_outbox": "FOREIGN KEY (outbox_id) REFERENCES outbox(id) ON DELETE CASCADE",
    "ck_attempt_sha_shape": "CHECK ((request_sha256 ~ '^[0-9a-f]{64}$'::text))",
    "ck_attempt_wire_vocab":
        "CHECK ((wire_version = ANY (ARRAY['legacy'::text, 'sequenced'::text])))",
}


def _validate_table_shape(conn) -> None:
    """Data-bearing shape is VALIDATED (refuse on mismatch); owned code objects are recreated."""
    problems: list[str] = []
    oid = conn.execute(sa.text(
        "SELECT to_regclass(format('%I.%I', current_schema(), 'outbox_delivery_attempts'))::oid"
    )).scalar()
    if oid is None:
        raise RuntimeError(f"migration 016: {_MISMATCH_SENTINEL} — attempt authority missing")
    condefs = {
        r.conname: r.condef
        for r in conn.execute(
            sa.text("SELECT conname, pg_get_constraintdef(oid) AS condef FROM pg_constraint "
                    "WHERE conrelid = :oid"),
            {"oid": oid},
        )
    }
    for name, want in _CANONICAL_CONSTRAINTS.items():
        got = condefs.pop(name, None)
        if got != want:
            problems.append(f"constraint {name}: expected {want!r}, found {got!r}")
    if condefs:
        problems.append(f"unexpected constraint(s): {sorted(condefs)}")
    if problems:
        raise RuntimeError(
            f"migration 016: {_MISMATCH_SENTINEL} — refusing to build authority on a drifted "
            f"table: {problems}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    _validate_table_shape(conn)

    # --- admission provenance (fast default: no row UPDATE, so 014's insert-only trigger holds).
    # Every pre-016 attempt row — including any inserted with an arbitrary token before the
    # admission trigger existed — becomes 'legacy_unverified': its staging claim is UNPROVEN, and
    # the taxonomy stops trusting bare existence.
    op.add_column(
        "outbox_delivery_attempts",
        sa.Column("admission", sa.Text(), nullable=False, server_default="legacy_unverified"),
    )
    op.create_check_constraint(
        "ck_attempt_admission_vocab", "outbox_delivery_attempts",
        "admission IN ('legacy_unverified','admission_v1')",
    )

    # --- canonical authority, recreated from scratch (never validated by name again) ---
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_attempts_immutable ON outbox_delivery_attempts")
    op.execute("DROP FUNCTION IF EXISTS outbox_attempts_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_attempts_admission ON outbox_delivery_attempts")
    op.execute("DROP FUNCTION IF EXISTS outbox_attempts_admission()")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_witness_guard()")

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
    op.execute(
        """
        CREATE FUNCTION outbox_attempts_admission() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent RECORD;
        BEGIN
            SELECT kind, status, claim_token INTO parent
            FROM outbox WHERE id = NEW.outbox_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % does not exist',
                    NEW.outbox_id;
            END IF;
            IF parent.kind <> 'decision_callback' THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % is kind %, only '
                    'decision_callback rows carry wire attempts', NEW.outbox_id, parent.kind;
            END IF;
            IF parent.status <> 'pending' THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % is %, not pending',
                    NEW.outbox_id, parent.status;
            END IF;
            IF parent.claim_token IS NULL OR parent.claim_token <> NEW.claim_token THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % is not claimed by %',
                    NEW.outbox_id, NEW.claim_token;
            END IF;
            -- admission is DECIDED HERE, never by the writer: whatever the INSERT carried is
            -- overwritten, so 'admission_v1' can only ever mean "this trigger admitted it".
            NEW.admission := 'admission_v1';
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_attempts_admission "
        "BEFORE INSERT ON outbox_delivery_attempts "
        "FOR EACH ROW EXECUTE FUNCTION outbox_attempts_admission()"
    )
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.witness_generation IS DISTINCT FROM OLD.witness_generation THEN
                RAISE EXCEPTION 'witness_generation is immutable (outbox row %): flipping it '
                    'manufactures or hides a not_accepted classification', OLD.id;
            END IF;
            -- Every FUTURE decision-callback delivery must carry its witness (re-audit F1 of
            -- 0c46443): transition-scoped, so already-delivered legacy NULL rows are untouched.
            IF OLD.kind = 'decision_callback' AND OLD.status = 'pending'
               AND NEW.status = 'delivered' AND NEW.callback_wire_sha256 IS NULL THEN
                RAISE EXCEPTION 'unwitnessed delivery refused (outbox row %): a decision '
                    'callback cannot become delivered without its wire witness', OLD.id;
            END IF;
            IF OLD.callback_wire_sha256 IS NOT NULL THEN
                IF NEW.callback_wire_sha256 IS DISTINCT FROM OLD.callback_wire_sha256
                   OR NEW.wire_version IS DISTINCT FROM OLD.wire_version THEN
                    RAISE EXCEPTION 'terminal wire witness is write-once (outbox row %): it is '
                        'the record of accepted bytes and may never be rewritten or cleared',
                        OLD.id;
                END IF;
            ELSIF NEW.callback_wire_sha256 IS NOT NULL THEN
                IF NOT (OLD.status = 'pending' AND NEW.status = 'delivered') THEN
                    RAISE EXCEPTION 'terminal wire witness may only be written on the '
                        'pending->delivered transition (outbox row %)', OLD.id;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM outbox_delivery_attempts a
                    WHERE a.outbox_id = OLD.id
                      AND a.claim_token = OLD.claim_token
                      AND a.request_sha256 = NEW.callback_wire_sha256
                      AND a.wire_version = NEW.wire_version
                ) THEN
                    RAISE EXCEPTION 'terminal wire witness refused (outbox row %): no attempt '
                        'under the live claim matches this digest/encoding — a terminal cannot '
                        'witness bytes nothing staged', OLD.id;
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_witness_guard BEFORE UPDATE ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_witness_guard()"
    )


def downgrade() -> None:
    conn = op.get_bind()
    # child-first: the writers' global lock order (attempts table, then parent row).
    op.execute("LOCK TABLE outbox_delivery_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    attempts = conn.execute(sa.text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one()
    digests = conn.execute(
        sa.text("SELECT count(*) FROM outbox WHERE callback_wire_sha256 IS NOT NULL")
    ).scalar_one()
    # NEGATIVE evidence counts too (re-audit F4 of 0c46443): an attempt_v1 decision callback with
    # no attempt and no digest is the durable proof that nothing was ever staged — the only state
    # licensing a non-delivery claim. Dropping witness_generation (014's downgrade) would erase
    # it and a re-upgrade would refill it as 'legacy', permanently. Refuse here, at the top of
    # the walk, so the destructive step is never reachable.
    negatives = conn.execute(sa.text(
        "SELECT count(*) FROM outbox WHERE kind = 'decision_callback' "
        "AND witness_generation = 'attempt_v1'"
    )).scalar_one()
    if attempts or digests or negatives:
        raise RuntimeError(
            f"migration 016 downgrade refused: {_REFUSED_SENTINEL} — {attempts} attempt row(s), "
            f"{digests} terminal digest(s), and {negatives} attempt-regime decision callback(s) "
            f"exist. Positive AND negative witness evidence is immutable; roll back by flag/image "
            f"on the compatible schema instead."
        )
    # unused database: restore 015's exact authority objects
    op.execute("DROP TRIGGER trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION outbox_witness_guard()")
    op.execute("DROP TRIGGER trg_outbox_attempts_admission ON outbox_delivery_attempts")
    op.execute("DROP FUNCTION outbox_attempts_admission()")
    op.drop_constraint("ck_attempt_admission_vocab", "outbox_delivery_attempts", type_="check")
    op.drop_column("outbox_delivery_attempts", "admission")
    op.execute(
        """
        CREATE FUNCTION outbox_attempts_admission() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent RECORD;
        BEGIN
            SELECT kind, status, claim_token INTO parent
            FROM outbox WHERE id = NEW.outbox_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % does not exist',
                    NEW.outbox_id;
            END IF;
            IF parent.kind <> 'decision_callback' THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % is kind %, only '
                    'decision_callback rows carry wire attempts', NEW.outbox_id, parent.kind;
            END IF;
            IF parent.status <> 'pending' THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % is %, not pending',
                    NEW.outbox_id, parent.status;
            END IF;
            IF parent.claim_token IS NULL OR parent.claim_token <> NEW.claim_token THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % is not claimed by %',
                    NEW.outbox_id, NEW.claim_token;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_attempts_admission "
        "BEFORE INSERT ON outbox_delivery_attempts "
        "FOR EACH ROW EXECUTE FUNCTION outbox_attempts_admission()"
    )
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.witness_generation IS DISTINCT FROM OLD.witness_generation THEN
                RAISE EXCEPTION 'witness_generation is immutable (outbox row %): flipping it '
                    'manufactures or hides a not_accepted classification', OLD.id;
            END IF;
            IF OLD.callback_wire_sha256 IS NOT NULL THEN
                IF NEW.callback_wire_sha256 IS DISTINCT FROM OLD.callback_wire_sha256
                   OR NEW.wire_version IS DISTINCT FROM OLD.wire_version THEN
                    RAISE EXCEPTION 'terminal wire witness is write-once (outbox row %): it is '
                        'the record of accepted bytes and may never be rewritten or cleared',
                        OLD.id;
                END IF;
            ELSIF NEW.callback_wire_sha256 IS NOT NULL THEN
                IF NOT (OLD.status = 'pending' AND NEW.status = 'delivered') THEN
                    RAISE EXCEPTION 'terminal wire witness may only be written on the '
                        'pending->delivered transition (outbox row %)', OLD.id;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM outbox_delivery_attempts a
                    WHERE a.outbox_id = OLD.id
                      AND a.claim_token = OLD.claim_token
                      AND a.request_sha256 = NEW.callback_wire_sha256
                      AND a.wire_version = NEW.wire_version
                ) THEN
                    RAISE EXCEPTION 'terminal wire witness refused (outbox row %): no attempt '
                        'under the live claim matches this digest/encoding — a terminal cannot '
                        'witness bytes nothing staged', OLD.id;
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_witness_guard BEFORE UPDATE ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_witness_guard()"
    )
