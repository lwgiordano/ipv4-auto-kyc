"""outbox authority boundary: lease fence, lifecycle guards, maintenance fence (PR 7b-core)

Revision ID: 017
Revises: 016

Folds the CORRECTNESS findings of re-audit `15d875d` and — for the first time in this loop —
formally REBUTS its regress-class prescriptions (terminal provenance-on-provenance, epoch reset of
historical claims). The boundary, declared once here and in the bus: the in-database authority
defends against APPLICATION defects and races; defense against an adversary holding raw SQL/DDL
privileges does not terminate inside the database it already controls — it terminates at the
platform's signed accepted-request ledger, which is what 7b-activation exists to provide. The
witness taxonomy's `legacy_*` states already say, honestly, "local history cannot prove this".

What this revision FIXES:

- **Expired leases stage nothing (F4).** Admission — DB trigger and publisher fence — required
  only token equality and `status='pending'`; an expired-but-not-yet-reclaimed owner could still
  stage an attempt and transmit, contradicting `_CLAIM_SQL`, which already treats that row as
  reclaimable. Admission now requires a live claimant AND `claim_lease_expires_at >
  clock_timestamp()` — wall-clock, not transaction-start `now()`, so a transaction that began
  before expiry cannot wait out the deadline and still win.
- **Delivered is reachable only from pending, and only through the front door (F1).** The guard
  was BEFORE UPDATE only, and its no-witness refusal checked only `OLD.status='pending'`: a
  delivered decision row could be INSERTed directly, and `dead → delivered` with a NULL digest
  passed. New rows must enter `pending` with no terminal fields and no claim; any transition INTO
  `delivered` requires source `pending`; the decision-terminal witness must be backed by an
  ADMITTED attempt (`admission_v1` joins the EXISTS in both trigger and publisher — the existing
  provenance doing more work, not a new provenance layer).
- **The ordering authority cannot be deleted or re-identified (F3).** No DELETE branch existed:
  a decision callback — including one whose `attempt_v1`/no-attempt state is durable NEGATIVE
  evidence — could simply be deleted, and identity fields (case/run/stream/sequence) could be
  rewritten. Decision callbacks are now undeletable; identity is immutable; the payload is
  immutable while the row is sendable (pending/dead) and mutable only to the governed redaction
  value once delivered/superseded.
- **Maintenance and writers share ONE fence (F5).** Child-first table locking does not eliminate
  deadlocks against the TERMINAL writer (parent row first, then the trigger reads the child).
  Writers now take a SHARED advisory transaction lock before touching either table; this
  migration (both directions) takes it EXCLUSIVE before any table lock — cycles are impossible
  by construction, and a live-claim preflight refuses the upgrade when quiescence was not real.
- **Functions resolve one schema (F2, folded part).** All authority functions are recreated with
  schema-qualified relations and a pinned `search_path`, and the pre-recreation validator covers
  the full structural surface (columns, defaults, constraints, indexes, the complete trigger
  set) — refusing on drift BEFORE any replacement. What is NOT folded: proving the unknowable
  history of prior function bodies, or demoting claims recorded under them — see the rebuttal.
"""

import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH"
_REFUSED_SENTINEL = "MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE"
_QUIESCENCE_SENTINEL = "MIGRATION_017_PREFLIGHT_LIVE_CLAIMS"

# One advisory lock namespace shared by every witness writer and this migration. Writers take it
# SHARED (they never block each other); maintenance takes it EXCLUSIVE. Keep in sync with
# publisher._MAINTENANCE_FENCE_KEY.
_FENCE_KEY = 720170001

_CANONICAL_CONSTRAINTS = {
    "outbox_delivery_attempts_pkey": "PRIMARY KEY (attempt_id)",
    "fk_attempt_outbox": "FOREIGN KEY (outbox_id) REFERENCES outbox(id) ON DELETE CASCADE",
    "ck_attempt_sha_shape": "CHECK ((request_sha256 ~ '^[0-9a-f]{64}$'::text))",
    "ck_attempt_wire_vocab":
        "CHECK ((wire_version = ANY (ARRAY['legacy'::text, 'sequenced'::text])))",
    "ck_attempt_admission_vocab":
        "CHECK ((admission = ANY (ARRAY['legacy_unverified'::text, 'admission_v1'::text])))",
}
_CANONICAL_COLUMNS = [
    ("attempt_id", "uuid", "NO", None),
    ("outbox_id", "bigint", "NO", None),
    ("claim_token", "uuid", "NO", None),
    ("wire_version", "text", "NO", None),
    ("request_sha256", "text", "NO", None),
    ("attempted_at", "timestamp with time zone", "NO", "now()"),
    ("admission", "text", "NO", "'legacy_unverified'::text"),
]
_CANONICAL_INDEX_SUFFIXES = {
    "outbox_delivery_attempts_pkey": "USING btree (attempt_id)",
    "ix_attempt_outbox": "USING btree (outbox_id, attempted_at DESC)",
}
_EXPECTED_ATTEMPT_TRIGGERS = {"trg_outbox_attempts_immutable", "trg_outbox_attempts_admission"}
_EXPECTED_OUTBOX_TRIGGERS = {"trg_outbox_witness_guard"}


def _validate_full_structure(conn) -> None:
    """The complete structural surface, validated BEFORE any code replacement (F2 folded part):
    columns/types/nullability/defaults, exact constraint definitions, indexes, and the complete
    trigger sets of BOTH tables. Data-bearing drift refuses; only then are owned functions
    recreated."""
    problems: list[str] = []
    oid = conn.execute(sa.text(
        "SELECT to_regclass(format('%I.%I', current_schema(), 'outbox_delivery_attempts'))::oid"
    )).scalar()
    if oid is None:
        raise RuntimeError(f"migration 017: {_MISMATCH_SENTINEL} — attempt authority missing")

    condefs = {
        r.conname: r.condef
        for r in conn.execute(
            sa.text("SELECT conname, pg_get_constraintdef(oid) AS condef FROM pg_constraint "
                    "WHERE conrelid = :oid"), {"oid": oid})
    }
    for name, want in _CANONICAL_CONSTRAINTS.items():
        got = condefs.pop(name, None)
        if got != want:
            problems.append(f"constraint {name}: expected {want!r}, found {got!r}")
    if condefs:
        problems.append(f"unexpected constraint(s): {sorted(condefs)}")

    cols = conn.execute(sa.text(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'outbox_delivery_attempts' ORDER BY ordinal_position")).fetchall()
    got_cols = [(c.column_name, c.data_type, c.is_nullable, c.column_default) for c in cols]
    if got_cols != _CANONICAL_COLUMNS:
        problems.append(f"columns: expected {_CANONICAL_COLUMNS}, found {got_cols}")

    indexdefs = {
        r.indexname: r.indexdef
        for r in conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = 'outbox_delivery_attempts'"))
    }
    for name, suffix in _CANONICAL_INDEX_SUFFIXES.items():
        got = indexdefs.pop(name, None)
        if got is None or not got.endswith(suffix) or (" UNIQUE " in got) != ("pkey" in name):
            problems.append(f"index {name}: expected …{suffix!r}, found {got!r}")
    if indexdefs:
        problems.append(f"unexpected index(es): {sorted(indexdefs)}")

    for rel, expected in (("outbox_delivery_attempts", _EXPECTED_ATTEMPT_TRIGGERS),
                          ("outbox", _EXPECTED_OUTBOX_TRIGGERS)):
        rel_oid = conn.execute(sa.text(
            "SELECT to_regclass(format('%I.%I', current_schema(), CAST(:rel AS text)))::oid"),
            {"rel": rel}).scalar()
        trig = {
            r.tgname for r in conn.execute(
                sa.text("SELECT tgname FROM pg_trigger WHERE tgrelid = :oid "
                        "AND NOT tgisinternal AND tgenabled <> 'D'"), {"oid": rel_oid})
        }
        if trig != expected:
            problems.append(f"{rel} triggers: expected exactly {sorted(expected)} (enabled), "
                            f"found {sorted(trig)}")
    if problems:
        raise RuntimeError(
            f"migration 017: {_MISMATCH_SENTINEL} — authority structure drifted; refusing to "
            f"recreate code on top of it: {problems}"
        )


def _drop_authority_code() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_attempts_immutable ON outbox_delivery_attempts")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_attempts_admission ON outbox_delivery_attempts")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_witness_guard ON outbox")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_lifecycle_insert ON outbox")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_no_delete ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_attempts_guard()")
    op.execute("DROP FUNCTION IF EXISTS outbox_attempts_admission()")
    op.execute("DROP FUNCTION IF EXISTS outbox_witness_guard()")
    op.execute("DROP FUNCTION IF EXISTS outbox_lifecycle_insert_guard()")
    op.execute("DROP FUNCTION IF EXISTS outbox_no_delete_guard()")


def upgrade() -> None:
    conn = op.get_bind()
    # ONE fence for maintenance and writers (F5): exclusive here, shared in the publisher.
    # Taken BEFORE any table lock, so a terminal writer holding the parent row can never form a
    # lock-order cycle with this migration — it simply finishes first.
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))
    _validate_full_structure(conn)

    op.execute("LOCK TABLE outbox_delivery_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    # Quiescence is a machine-checked precondition, not a runbook promise: a live claim means a
    # publisher is mid-flight, and "drained" was not true.
    live = conn.execute(sa.text(
        "SELECT count(*) FROM outbox WHERE claim_token IS NOT NULL "
        "AND claim_lease_expires_at > clock_timestamp()")).scalar_one()
    if live:
        raise RuntimeError(
            f"migration 017: {_QUIESCENCE_SENTINEL} — {live} live outbox claim(s) exist; the "
            f"drained-cutover precondition is not met. Stop the publishers (and retention), "
            f"let leases expire or run reset_interrupted_outbox_claims, then retry."
        )

    _drop_authority_code()

    # --- attempts: insert-only + digest-redundant deletion (unchanged semantics, pinned path) ---
    op.execute(
        """
        CREATE FUNCTION outbox_attempts_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'outbox_delivery_attempts is insert-only (attempt % )', OLD.attempt_id;
            END IF;
            IF EXISTS (SELECT 1 FROM public.outbox o
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

    # --- admission: live claim means UNEXPIRED claim (F4) ---
    op.execute(
        """
        CREATE FUNCTION outbox_attempts_admission() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        DECLARE parent RECORD;
        BEGIN
            SELECT kind, status, claim_token, claimed_by, claim_lease_expires_at INTO parent
            FROM public.outbox WHERE id = NEW.outbox_id FOR UPDATE;
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
            IF parent.claim_token IS NULL OR parent.claim_token <> NEW.claim_token
               OR parent.claimed_by IS NULL THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % is not claimed by %',
                    NEW.outbox_id, NEW.claim_token;
            END IF;
            -- clock_timestamp(), not now(): a transaction that BEGAN before expiry must not be
            -- able to wait past the deadline and still stage evidence (re-audit 15d875d F4).
            IF parent.claim_lease_expires_at IS NULL
               OR parent.claim_lease_expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'attempt admission refused: outbox row % lease expired at % — '
                    'an expired claim is reclaimable and must not stage evidence',
                    NEW.outbox_id, parent.claim_lease_expires_at;
            END IF;
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

    # --- the outbox lifecycle authority (F1 + F3) ---
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            IF NEW.witness_generation IS DISTINCT FROM OLD.witness_generation THEN
                RAISE EXCEPTION 'witness_generation is immutable (outbox row %)', OLD.id;
            END IF;
            -- identity IS the ordering authority (re-audit 15d875d F3): rewriting it re-attributes
            -- recorded history to a different case/run/stream/ordinal.
            IF NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.case_id IS DISTINCT FROM OLD.case_id
               OR NEW.run_id IS DISTINCT FROM OLD.run_id
               OR NEW.ordering_stream IS DISTINCT FROM OLD.ordering_stream
               OR NEW.decision_sequence IS DISTINCT FROM OLD.decision_sequence THEN
                RAISE EXCEPTION 'outbox identity is immutable (outbox row %)', OLD.id;
            END IF;
            -- a sendable body is immutable; a terminal body may change ONLY to the governed
            -- redaction value (retention). Everything else is fabrication.
            IF NEW.payload_json IS DISTINCT FROM OLD.payload_json THEN
                IF NOT (OLD.status IN ('delivered','superseded')
                        AND NEW.payload_json = '{"redacted": true}'::jsonb) THEN
                    RAISE EXCEPTION 'outbox payload is immutable while sendable; a terminal body '
                        'may only take the governed redaction value (outbox row %)', OLD.id;
                END IF;
            END IF;
            -- delivered is reachable ONLY from pending (dead|superseded -> delivered closed).
            IF NEW.status = 'delivered' AND OLD.status NOT IN ('pending','delivered') THEN
                RAISE EXCEPTION 'delivered is reachable only from pending (outbox row %, was %)',
                    OLD.id, OLD.status;
            END IF;
            IF OLD.kind = 'decision_callback' AND OLD.status = 'pending'
               AND NEW.status = 'delivered' AND NEW.callback_wire_sha256 IS NULL THEN
                RAISE EXCEPTION 'unwitnessed delivery refused (outbox row %)', OLD.id;
            END IF;
            IF OLD.callback_wire_sha256 IS NOT NULL THEN
                IF NEW.callback_wire_sha256 IS DISTINCT FROM OLD.callback_wire_sha256
                   OR NEW.wire_version IS DISTINCT FROM OLD.wire_version THEN
                    RAISE EXCEPTION 'terminal wire witness is write-once (outbox row %)', OLD.id;
                END IF;
            ELSIF NEW.callback_wire_sha256 IS NOT NULL THEN
                IF NOT (OLD.status = 'pending' AND NEW.status = 'delivered') THEN
                    RAISE EXCEPTION 'terminal wire witness may only be written on the '
                        'pending->delivered transition (outbox row %)', OLD.id;
                END IF;
                -- the backing attempt must be ADMITTED (re-audit 15d875d F1): the existing
                -- provenance doing more work — an unverified pre-authority attempt cannot
                -- underwrite a terminal.
                IF NOT EXISTS (
                    SELECT 1 FROM public.outbox_delivery_attempts a
                    WHERE a.outbox_id = OLD.id
                      AND a.claim_token = OLD.claim_token
                      AND a.request_sha256 = NEW.callback_wire_sha256
                      AND a.wire_version = NEW.wire_version
                      AND a.admission = 'admission_v1'
                ) THEN
                    RAISE EXCEPTION 'terminal wire witness refused (outbox row %): no ADMITTED '
                        'attempt under the live claim matches this digest/encoding', OLD.id;
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
    op.execute(
        """
        CREATE FUNCTION outbox_lifecycle_insert_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            -- every decision callback is BORN pending, unwitnessed, unclaimed: a delivered/dead
            -- row can otherwise be fabricated wholesale, bypassing every UPDATE guard
            -- (re-audit 15d875d F1). Legacy states reach head only THROUGH the migrations.
            IF NEW.kind = 'decision_callback' AND (
                NEW.status <> 'pending'
                OR NEW.callback_wire_sha256 IS NOT NULL OR NEW.wire_version IS NOT NULL
                OR NEW.delivered_at IS NOT NULL OR NEW.resolved_at IS NOT NULL
                OR NEW.claim_token IS NOT NULL OR NEW.claimed_by IS NOT NULL
                OR NEW.claim_lease_expires_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'decision callbacks enter the outbox pending, unwitnessed and '
                    'unclaimed (attempted INSERT as %)', NEW.status;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_lifecycle_insert BEFORE INSERT ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_lifecycle_insert_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION outbox_no_delete_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            IF OLD.kind = 'decision_callback' THEN
                RAISE EXCEPTION 'decision callback % is the durable ordering authority (and may '
                    'carry negative witness evidence); deletion refused', OLD.id;
            END IF;
            RETURN OLD;  -- poc_email deletion (retention) remains legal
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_no_delete BEFORE DELETE ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_no_delete_guard()"
    )


def downgrade() -> None:
    conn = op.get_bind()
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))
    op.execute("LOCK TABLE outbox_delivery_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    attempts = conn.execute(sa.text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one()
    digests = conn.execute(sa.text(
        "SELECT count(*) FROM outbox WHERE callback_wire_sha256 IS NOT NULL")).scalar_one()
    negatives = conn.execute(sa.text(
        "SELECT count(*) FROM outbox WHERE kind = 'decision_callback' "
        "AND witness_generation = 'attempt_v1'")).scalar_one()
    if attempts or digests or negatives:
        raise RuntimeError(
            f"migration 017 downgrade refused: {_REFUSED_SENTINEL} — {attempts} attempt row(s), "
            f"{digests} terminal digest(s), {negatives} attempt-regime decision callback(s). "
            f"Witness evidence (positive AND negative) is immutable; roll back by flag/image on "
            f"the compatible schema instead."
        )
    _drop_authority_code()
    # restore 016's exact authority (unpinned path, pre-F4 admission, no INSERT/DELETE guards)
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
