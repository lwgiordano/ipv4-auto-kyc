"""witness authority hardening: admission, immutability, deep validation (PR 7b-core)

Revision ID: 015
Revises: 014

`014` created the evidence tables and the first guards; this revision makes them an AUTHORITY —
meaning the database itself refuses every fabrication path the re-audit (`4dfdf8a`) demonstrated,
instead of trusting application discipline:

- **Attempt admission** (BEFORE INSERT): an attempt may only be recorded for a live-claimed,
  pending decision callback, under that row's exact claim token. `014`'s trigger protected
  UPDATE/DELETE but not INSERT, so raw SQL could manufacture `send_intent_witnessed` for a row
  nothing ever claimed. The parent row is locked FOR UPDATE first, so admission serializes with
  claim changes and the check cannot race a reclaim.
- **Witness immutability** (BEFORE UPDATE on outbox): `witness_generation` is immutable for the
  row's life (flipping it manufactures `not_accepted` or hides it); the terminal digest is
  WRITE-ONCE — NULL→set only on the pending→delivered transition and only when a matching attempt
  (same outbox, same claim, same digest, same encoding) exists; a set digest can never be
  rewritten or cleared (clearing one after retention pruned the redundant attempts would convert
  a locally accepted delivery into a false `not_accepted`). Already-delivered legacy NULLs are
  grandfathered as NULL forever — evidence is never fabricated after the fact.
- **Deep re-validation** of the attempt authority: `014` accepted an amended-history table on
  name/substring checks, which a same-named weakened CHECK or an extra `UNIQUE(outbox_id)` could
  satisfy. This revision re-validates by exact normalized definition, schema-bound through
  `current_schema()`, and rejects UNEXPECTED write-affecting objects, then fails closed on any
  mismatch with the same operator sentinel.

`014` is published and is not edited; its admission gap is closed here, ahead of any production
deployment of the chain. Downgrade takes locks CHILD-FIRST (attempts, then outbox) — the opposite
of `014`'s order, which can deadlock (40P01) against a live attempt INSERT that holds the child
table and waits on the parent. With witnesses present it refuses; `014`'s deadlock-prone downgrade
is only reachable after this one succeeds, i.e. on a witness-free database with writers drained.
"""

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None

# Same operator sentinel as 014's validator: one token to grep for, wherever the mismatch is
# caught. (The sentinel names the CONDITION, not the revision that detected it.)
_MISMATCH_SENTINEL = "MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH"
_REFUSED_SENTINEL = "MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE"

# Exact normalized shapes, as PostgreSQL renders them (pg_get_constraintdef / pg_get_indexdef on
# a table created by 014's DDL). Compared for EQUALITY — a same-named weakened constraint or a
# widened index differs in its rendered definition and fails.
_CANONICAL_CONSTRAINTS = {
    "outbox_delivery_attempts_pkey": "PRIMARY KEY (attempt_id)",
    "fk_attempt_outbox": "FOREIGN KEY (outbox_id) REFERENCES outbox(id) ON DELETE CASCADE",
    "ck_attempt_sha_shape": "CHECK ((request_sha256 ~ '^[0-9a-f]{64}$'::text))",
    "ck_attempt_wire_vocab":
        "CHECK ((wire_version = ANY (ARRAY['legacy'::text, 'sequenced'::text])))",
}
_CANONICAL_INDEX_SUFFIXES = {
    # pg_get_indexdef prefixes the schema per search_path; compare the stable suffix.
    "outbox_delivery_attempts_pkey":
        "USING btree (attempt_id)",
    "ix_attempt_outbox":
        "USING btree (outbox_id, attempted_at DESC)",
}
_EXPECTED_TRIGGERS = {"trg_outbox_attempts_immutable"}  # 014's; 015 adds its own AFTER validating


def _revalidate_attempt_authority(conn) -> None:
    problems: list[str] = []

    # ::oid — psycopg renders a bare regclass as its NAME, which cannot round-trip back into
    # an oid-typed parameter; the integer can.
    oid = conn.execute(sa.text(
        "SELECT to_regclass(format('%I.%I', current_schema(), 'outbox_delivery_attempts'))::oid"
    )).scalar()
    if oid is None:
        raise RuntimeError(
            f"migration 015: {_MISMATCH_SENTINEL} — outbox_delivery_attempts missing from "
            f"current_schema(); 014 must have created or adopted it"
        )

    condefs = {
        r.conname: r.condef
        for r in conn.execute(
            sa.text("SELECT conname, pg_get_constraintdef(oid) AS condef FROM pg_constraint "
                    "WHERE conrelid = :oid AND contype IN ('p','f','c','u','x')"),
            {"oid": oid},
        )
    }
    for name, want in _CANONICAL_CONSTRAINTS.items():
        got = condefs.pop(name, None)
        if got != want:
            problems.append(f"constraint {name}: expected {want!r}, found {got!r}")
    if condefs:  # anything left is an object 013/014 never created — e.g. UNIQUE(outbox_id)
        problems.append(f"unexpected constraint(s): {sorted(condefs)}")

    indexdefs = {
        r.indexname: r.indexdef
        for r in conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = 'outbox_delivery_attempts'"
        ))
    }
    for name, suffix in _CANONICAL_INDEX_SUFFIXES.items():
        got = indexdefs.pop(name, None)
        if got is None or not got.endswith(suffix) or (" UNIQUE " in got) != ("pkey" in name):
            problems.append(f"index {name}: expected …{suffix!r} (unique only for the PK), "
                            f"found {got!r}")
    if indexdefs:
        problems.append(f"unexpected index(es): {sorted(indexdefs)}")

    triggers = {
        r.tgname
        for r in conn.execute(
            sa.text("SELECT tgname FROM pg_trigger WHERE tgrelid = :oid AND NOT tgisinternal"),
            {"oid": oid},
        )
    }
    if triggers != _EXPECTED_TRIGGERS:
        problems.append(f"triggers: expected exactly {sorted(_EXPECTED_TRIGGERS)}, "
                        f"found {sorted(triggers)}")

    cols = conn.execute(sa.text(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'outbox_delivery_attempts' ORDER BY ordinal_position"
    )).fetchall()
    want_cols = [
        ("attempt_id", "uuid", "NO", None),
        ("outbox_id", "bigint", "NO", None),
        ("claim_token", "uuid", "NO", None),
        ("wire_version", "text", "NO", None),
        ("request_sha256", "text", "NO", None),
        ("attempted_at", "timestamp with time zone", "NO", "now()"),
    ]
    got_cols = [(c.column_name, c.data_type, c.is_nullable, c.column_default) for c in cols]
    if got_cols != want_cols:
        problems.append(f"columns: expected {want_cols}, found {got_cols}")

    if problems:
        raise RuntimeError(
            f"migration 015: {_MISMATCH_SENTINEL} — the adopted attempt authority does not match "
            f"the canonical shape exactly; evidence written into a drifted table cannot be "
            f"trusted. Mismatches: {problems}"
        )


def upgrade() -> None:
    _revalidate_attempt_authority(op.get_bind())

    # --- attempt ADMISSION: evidence can only enter under a live claim ---
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

    # --- witness IMMUTABILITY on outbox ---
    # Deliberately NOT requiring witness_generation='attempt_v1' for admission above: a legacy
    # pending/dead row redelivered by the modern publisher records an attempt and becomes
    # delivery_witnessed going forward — requiring attempt_v1 would strand every legacy row as
    # permanently undeliverable. Generation gates only the not_accepted CLASSIFICATION.
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


def downgrade() -> None:
    conn = op.get_bind()
    # CHILD-FIRST lock order (the one every writer follows: attempts table, then the parent row
    # via the admission trigger). 014's parent-first order can cycle with a live attempt INSERT.
    op.execute("LOCK TABLE outbox_delivery_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    attempts = conn.execute(sa.text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one()
    digests = conn.execute(
        sa.text("SELECT count(*) FROM outbox WHERE callback_wire_sha256 IS NOT NULL")
    ).scalar_one()
    if attempts or digests:
        raise RuntimeError(
            f"migration 015 downgrade refused: {_REFUSED_SENTINEL} — {attempts} attempt row(s) "
            f"and {digests} terminal digest(s) exist; witness evidence is immutable. Roll back "
            f"by flag/image on the compatible schema instead."
        )
    op.execute("DROP TRIGGER trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION outbox_witness_guard()")
    op.execute("DROP TRIGGER trg_outbox_attempts_admission ON outbox_delivery_attempts")
    op.execute("DROP FUNCTION outbox_attempts_admission()")
