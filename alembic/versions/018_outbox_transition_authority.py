"""outbox transition authority: dual-table manifest, terminal finality, manual pointer (PR 7b-core)

Revision ID: 018
Revises: 017

Repairs the eight findings of re-audit `cbb783b`, which adjudicated the `017` rebuttals: R1
ACCEPTED (no `terminal_v1` — a local bit cannot prove a network fact against an adversary holding
the same database; the signed platform ledger is the terminating authority), R2 ACCEPTED **for
unknowable prior execution history but NOT for observable present structure**. That carve-out is
correct and is what this revision implements: PostgreSQL cannot prove what a function DID before
the verifier ran, but columns, constraints, indexes, trigger wiring and current definitions are
directly observable — and `017` only observed half of them.

What this revision FIXES:

- **The manifest covers BOTH authority tables (F1).** `017`'s validator checked columns,
  constraints and indexes for `outbox_delivery_attempts` only; for `outbox` it compared enabled
  trigger NAMES. Dropping `fk_outbox_decision_triple` or `ck_outbox_wire_witness_delivered` at
  `016` therefore upgraded cleanly, and a same-name no-op witness function passed and was silently
  replaced. One canonical manifest now pins, for both tables: ordered columns with
  types/nullability/defaults, every named constraint's exact definition, every index's exact
  definition, the complete enabled trigger set with exact `pg_get_triggerdef`, and each owned
  function's normalized-body digest plus its pinned `search_path`. Drift refuses with a stable
  sentinel BEFORE any DDL. This is ordinary partial-deploy drift — inside the boundary, not the
  raw-DDL adversary R2 excludes.
- **Terminal means terminal (F2).** `017`'s guard constrained only transitions INTO `delivered`,
  so a `superseded` callback accepted `status='pending', resolved_at=NULL` — becoming claimable and
  transmittable again — plus `superseded → dead`, terminal-timestamp rewrites, and the same
  resurrection for a legacy `delivered` row whose NULL digest left the write-once branch nothing to
  freeze. State-shape CHECKs constrain shape, never history. There is now ONE exhaustive per-kind
  transition matrix (decision callbacks: `pending → pending|dead|delivered|superseded` and the
  governed `dead → pending` retry, with `delivered`/`superseded` FINAL; poc emails: their
  documented pending/retry/dead/delivered paths). On a terminal row, status, terminal timestamps,
  claim tuple, witness, identity, attempt counter and error text are all frozen; the ONLY permitted
  write is the governed payload redaction.
- **The unsafe downgrade is unreachable (F3).** `017`'s witness-free downgrade recreated `016`'s
  functions with their historical unqualified bodies and caller-controlled `search_path` — a clean
  rollback walked straight into shadow-relation vulnerability. `017` is published and is never
  edited, so `018` closes the walk instead: once installed it is forward-only, and the supported
  rollback is the prior compatible image on schema `018`. Unsafe historical text is not a
  compatibility requirement.
- **`cases.latest_manual_decision_row_id` (F6).** `017` sourced sticky manual attribution with
  `ORDER BY decisions.id DESC` — and `decisions.id` is a random UUID hex, so the "latest" manual
  approval was the lexically largest one. Attribution now follows a trigger-maintained pointer,
  written under the same INSERT path as the automatic pointer; multi-manual legacy histories that
  cannot be ordered are left NULL and read as an explicit unresolved state rather than guessed.

Kept from `017` unchanged: the shared advisory fence (writers shared, maintenance exclusive), the
live-claim quiescence preflight, admission's unexpired-lease requirement, the INSERT/DELETE guards,
identity immutability, and the admitted-attempt terminal EXISTS.
"""

import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH"
_QUIESCENCE_SENTINEL = "MIGRATION_018_PREFLIGHT_LIVE_CLAIMS"
_FORWARD_ONLY_SENTINEL = "MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY"

# Same namespace as 017 / publisher._MAINTENANCE_FENCE_KEY / retention. Writers take it SHARED,
# maintenance EXCLUSIVE; see kyc_tool.outbox.fence (the single definition both sides import).
_FENCE_KEY = 720170001

_ATTEMPT_COLUMNS = [
    ("attempt_id", "uuid", "NO", None),
    ("outbox_id", "bigint", "NO", None),
    ("claim_token", "uuid", "NO", None),
    ("wire_version", "text", "NO", None),
    ("request_sha256", "text", "NO", None),
    ("attempted_at", "timestamp with time zone", "NO", "now()"),
    ("admission", "text", "NO", "'legacy_unverified'::text"),
]
_OUTBOX_COLUMNS = [
    ("id", "bigint", "NO", "nextval('outbox_id_seq'::regclass)"),
    ("kind", "text", "NO", None),
    ("case_id", "text", "NO", None),
    ("run_id", "text", "YES", None),
    ("payload_json", "jsonb", "NO", "'{}'::jsonb"),
    ("status", "text", "NO", "'pending'::text"),
    ("attempts", "integer", "NO", "0"),
    ("next_attempt_at", "timestamp with time zone", "NO", "now()"),
    ("delivered_at", "timestamp with time zone", "YES", None),
    ("last_error", "text", "YES", None),
    ("created_at", "timestamp with time zone", "NO", "now()"),
    ("ordering_stream", "text", "NO", None),
    ("decision_sequence", "bigint", "YES", None),
    ("resolved_at", "timestamp with time zone", "YES", None),
    ("claim_lease_expires_at", "timestamp with time zone", "YES", None),
    ("claim_token", "uuid", "YES", None),
    ("claimed_by", "text", "YES", None),
    ("callback_wire_sha256", "text", "YES", None),
    ("wire_version", "text", "YES", None),
    ("witness_generation", "text", "NO", "'legacy'::text"),
]

_ATTEMPT_CONSTRAINTS = {
    "outbox_delivery_attempts_pkey": "PRIMARY KEY (attempt_id)",
    "fk_attempt_outbox": "FOREIGN KEY (outbox_id) REFERENCES outbox(id) ON DELETE CASCADE",
    "ck_attempt_sha_shape": "CHECK ((request_sha256 ~ '^[0-9a-f]{64}$'::text))",
    "ck_attempt_wire_vocab":
        "CHECK ((wire_version = ANY (ARRAY['legacy'::text, 'sequenced'::text])))",
    "ck_attempt_admission_vocab":
        "CHECK ((admission = ANY (ARRAY['legacy_unverified'::text, 'admission_v1'::text])))",
}
_OUTBOX_CONSTRAINTS = {
    "outbox_pkey": "PRIMARY KEY (id)",
    "fk_outbox_case_id": "FOREIGN KEY (case_id) REFERENCES cases(id)",
    "fk_outbox_decision_triple":
        "FOREIGN KEY (run_id, case_id, decision_sequence) "
        "REFERENCES decisions(run_id, case_id, decision_sequence)",
    "ck_outbox_kind_vocab":
        "CHECK ((kind = ANY (ARRAY['decision_callback'::text, 'poc_email'::text])))",
    "ck_outbox_ordering_stream_vocab":
        "CHECK ((ordering_stream = ANY (ARRAY['decision'::text, 'email'::text])))",
    "ck_outbox_witness_generation":
        "CHECK ((witness_generation = ANY (ARRAY['legacy'::text, 'attempt_v1'::text])))",
    "ck_outbox_wire_witness_delivered":
        "CHECK (((callback_wire_sha256 IS NULL) OR (status = 'delivered'::text)))",
    "ck_outbox_kind_stream_identity":
        "CHECK ((((kind = 'decision_callback'::text) AND (ordering_stream = 'decision'::text) AND "
        "(run_id IS NOT NULL) AND (decision_sequence IS NOT NULL) AND (decision_sequence > 0)) OR "
        "((kind = 'poc_email'::text) AND (ordering_stream = 'email'::text) AND (run_id IS NULL) "
        "AND (decision_sequence IS NULL))))",
    "ck_outbox_wire_witness":
        "CHECK ((((callback_wire_sha256 IS NULL) = (wire_version IS NULL)) AND "
        "((callback_wire_sha256 IS NULL) OR (callback_wire_sha256 ~ '^[0-9a-f]{64}$'::text)) AND "
        "((wire_version IS NULL) OR (wire_version = ANY (ARRAY['legacy'::text, "
        "'sequenced'::text]))) AND ((callback_wire_sha256 IS NULL) OR "
        "(kind = 'decision_callback'::text))))",
    "ck_outbox_status_lifecycle":
        "CHECK (((status = ANY (ARRAY['pending'::text, 'delivered'::text, 'dead'::text, "
        "'superseded'::text])) AND ((status <> 'pending'::text) OR ((delivered_at IS NULL) AND "
        "(resolved_at IS NULL) AND (((claim_token IS NULL) AND (claim_lease_expires_at IS NULL) "
        "AND (claimed_by IS NULL)) OR ((claim_token IS NOT NULL) AND "
        "(claim_lease_expires_at IS NOT NULL) AND (claimed_by IS NOT NULL))))) AND "
        "((status <> 'delivered'::text) OR ((delivered_at IS NOT NULL) AND (resolved_at IS NULL) "
        "AND (claim_token IS NULL) AND (claim_lease_expires_at IS NULL) AND "
        "(claimed_by IS NULL))) AND ((status <> 'dead'::text) OR ((delivered_at IS NULL) AND "
        "(resolved_at IS NULL) AND (claim_token IS NULL) AND (claim_lease_expires_at IS NULL) AND "
        "(claimed_by IS NULL))) AND ((status <> 'superseded'::text) OR ((delivered_at IS NULL) AND "
        "(resolved_at IS NOT NULL) AND (claim_token IS NULL) AND "
        "(claim_lease_expires_at IS NULL) AND (claimed_by IS NULL))) AND "
        "((status <> 'superseded'::text) OR (kind = 'decision_callback'::text))))",
}

_ATTEMPT_INDEXES = {
    "outbox_delivery_attempts_pkey":
        "CREATE UNIQUE INDEX outbox_delivery_attempts_pkey ON public.outbox_delivery_attempts "
        "USING btree (attempt_id)",
    "ix_attempt_outbox":
        "CREATE INDEX ix_attempt_outbox ON public.outbox_delivery_attempts USING btree "
        "(outbox_id, attempted_at DESC)",
}
_OUTBOX_INDEXES = {
    "outbox_pkey": "CREATE UNIQUE INDEX outbox_pkey ON public.outbox USING btree (id)",
    "ix_outbox_case_id": "CREATE INDEX ix_outbox_case_id ON public.outbox USING btree (case_id)",
    "ix_outbox_claim":
        "CREATE INDEX ix_outbox_claim ON public.outbox USING btree (next_attempt_at) "
        "WHERE (status = 'pending'::text)",
    "ix_outbox_live_status":
        "CREATE INDEX ix_outbox_live_status ON public.outbox USING btree (status) "
        "WHERE (status = ANY (ARRAY['pending'::text, 'dead'::text]))",
    "ix_outbox_stream_claim":
        "CREATE INDEX ix_outbox_stream_claim ON public.outbox USING btree "
        "(case_id, ordering_stream, next_attempt_at) WHERE (status = 'pending'::text)",
    "uq_outbox_decision_callback_run":
        "CREATE UNIQUE INDEX uq_outbox_decision_callback_run ON public.outbox USING btree "
        "(run_id) WHERE (kind = 'decision_callback'::text)",
}

_ATTEMPT_TRIGGERS = {
    "trg_outbox_attempts_admission":
        "CREATE TRIGGER trg_outbox_attempts_admission BEFORE INSERT ON "
        "public.outbox_delivery_attempts FOR EACH ROW EXECUTE FUNCTION "
        "outbox_attempts_admission()",
    "trg_outbox_attempts_immutable":
        "CREATE TRIGGER trg_outbox_attempts_immutable BEFORE DELETE OR UPDATE ON "
        "public.outbox_delivery_attempts FOR EACH ROW EXECUTE FUNCTION outbox_attempts_guard()",
}
_OUTBOX_TRIGGERS = {
    "trg_outbox_lifecycle_insert":
        "CREATE TRIGGER trg_outbox_lifecycle_insert BEFORE INSERT ON public.outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_lifecycle_insert_guard()",
    "trg_outbox_no_delete":
        "CREATE TRIGGER trg_outbox_no_delete BEFORE DELETE ON public.outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_no_delete_guard()",
    "trg_outbox_witness_guard":
        "CREATE TRIGGER trg_outbox_witness_guard BEFORE UPDATE ON public.outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_witness_guard()",
}

# sha256 of each owned function's whitespace-normalized body exactly as `017` installed it. A
# same-name no-op — the mutilation that walked past `017`'s name-only check — changes the digest.
# These prove the OBSERVED PRESENT definition, never prior execution history (rebuttal R2, as
# adjudicated): what a body did between migrations is not recorded anywhere in PostgreSQL.
_FUNCTION_BODY_SHA256 = {
    "outbox_attempts_admission":
        "6efacbd11eb8b2cc097d940013faead2e1af06adbb666b6e335cf6248bff28c7",
    "outbox_attempts_guard":
        "484829cd1546c28f3b9f887ea8e8c9fbfb9a280c2305975cff9e4fff0bc119c9",
    "outbox_lifecycle_insert_guard":
        "ff3a3bd81880de5dfc52340c9dd6e3ea3d15afb36214b62d3c5d603673f543ab",
    "outbox_no_delete_guard":
        "b683cbcd772fa5701642ee7992f6bb66105f7c9ddc911d3c2b03c3995b5109ef",
    "outbox_witness_guard":
        "7fd8b06062cb25537e021a2aa4448c05db66094ad3146bd09adb23d5bd9cd5ff",
}
_EXPECTED_SEARCH_PATH = "search_path=public, pg_catalog"


def _body_digest(prosrc: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", prosrc).strip().encode()).hexdigest()


def _validate_relation(conn, rel: str, columns, constraints, indexes, triggers) -> list[str]:
    """One reusable manifest check for ONE authority relation. Returns problems, raises nothing —
    the caller refuses once, with every problem named, before any DDL runs."""
    problems: list[str] = []
    oid = conn.execute(sa.text(
        "SELECT to_regclass(format('%I.%I', current_schema(), CAST(:rel AS text)))::oid"),
        {"rel": rel}).scalar()
    if oid is None:
        return [f"{rel}: relation missing from current_schema()"]

    got_cols = [
        (r.column_name, r.data_type, r.is_nullable, r.column_default)
        for r in conn.execute(sa.text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = CAST(:rel AS text) ORDER BY ordinal_position"), {"rel": rel})
    ]
    if got_cols != columns:
        problems.append(f"{rel} columns: expected {columns}, found {got_cols}")

    condefs = {
        r.conname: r.condef
        for r in conn.execute(sa.text(
            "SELECT conname, pg_get_constraintdef(oid) AS condef FROM pg_constraint "
            "WHERE conrelid = :oid"), {"oid": oid})
    }
    for name, want in constraints.items():
        got = condefs.pop(name, None)
        if got != want:
            problems.append(f"{rel} constraint {name}: expected {want!r}, found {got!r}")
    if condefs:
        problems.append(f"{rel} unexpected constraint(s): {sorted(condefs)}")

    indexdefs = {
        r.indexname: r.indexdef
        for r in conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = CAST(:rel AS text)"), {"rel": rel})
    }
    for name, want in indexes.items():
        got = indexdefs.pop(name, None)
        if got != want:
            problems.append(f"{rel} index {name}: expected {want!r}, found {got!r}")
    if indexdefs:
        problems.append(f"{rel} unexpected index(es): {sorted(indexdefs)}")

    # timing, events, target relation AND target function all live inside pg_get_triggerdef, so
    # an exact compare covers redirection, retiming and re-eventing in one predicate. A DISABLED
    # trigger is a missing trigger: tgenabled <> 'D' is part of the identity being pinned.
    trigdefs = {
        r.tgname: r.tdef
        for r in conn.execute(sa.text(
            "SELECT tgname, pg_get_triggerdef(oid) AS tdef FROM pg_trigger "
            "WHERE tgrelid = :oid AND NOT tgisinternal AND tgenabled <> 'D'"), {"oid": oid})
    }
    for name, want in triggers.items():
        got = trigdefs.pop(name, None)
        if got != want:
            problems.append(f"{rel} trigger {name}: expected {want!r}, found {got!r}")
    if trigdefs:
        problems.append(f"{rel} unexpected/extra enabled trigger(s): {sorted(trigdefs)}")
    return problems


def _validate_authority_manifest(conn) -> None:
    """The complete OBSERVABLE authority surface of BOTH tables plus every owned function, checked
    before any code is replaced (re-audit `cbb783b` F1). `017` validated the child table's shape
    and the parent's trigger NAMES; a dropped parent FK/CHECK or a same-name no-op function walked
    straight through."""
    problems = _validate_relation(
        conn, "outbox_delivery_attempts",
        _ATTEMPT_COLUMNS, _ATTEMPT_CONSTRAINTS, _ATTEMPT_INDEXES, _ATTEMPT_TRIGGERS)
    problems += _validate_relation(
        conn, "outbox", _OUTBOX_COLUMNS, _OUTBOX_CONSTRAINTS, _OUTBOX_INDEXES, _OUTBOX_TRIGGERS)

    procs = {
        r.proname: (r.prosrc, list(r.proconfig or []))
        for r in conn.execute(sa.text(
            "SELECT p.proname, p.prosrc, p.proconfig FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname = ANY(:names)"),
            {"names": sorted(_FUNCTION_BODY_SHA256)})
    }
    for name, want_sha in _FUNCTION_BODY_SHA256.items():
        found = procs.get(name)
        if found is None:
            problems.append(f"function {name}: missing")
            continue
        prosrc, proconfig = found
        got_sha = _body_digest(prosrc)
        if got_sha != want_sha:
            problems.append(
                f"function {name}: body digest {got_sha} != expected {want_sha} (the definition "
                f"in the database is not the one revision 017 installed)")
        if _EXPECTED_SEARCH_PATH not in proconfig:
            problems.append(
                f"function {name}: search_path not pinned to {_EXPECTED_SEARCH_PATH!r} "
                f"(found {proconfig!r})")

    if problems:
        raise RuntimeError(
            f"migration 018: {_MISMATCH_SENTINEL} — the outbox authority surface is not the one "
            f"017 installed; refusing to build transition authority on top of it. Reconcile the "
            f"environment (or restore from the reviewed image) and retry. Problems: {problems}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    # Exclusive on the SAME fence every witness writer and retention now takes shared.
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))
    _validate_authority_manifest(conn)

    op.execute("LOCK TABLE outbox_delivery_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    live = conn.execute(sa.text(
        "SELECT count(*) FROM outbox WHERE claim_token IS NOT NULL "
        "AND claim_lease_expires_at > clock_timestamp()")).scalar_one()
    if live:
        raise RuntimeError(
            f"migration 018: {_QUIESCENCE_SENTINEL} — {live} live outbox claim(s) exist; the "
            f"drained-cutover precondition is not met. Stop the publishers and retention, let "
            f"leases expire or run reset_interrupted_outbox_claims, then retry."
        )

    # --- F6: the manual-attribution pointer, maintained the way the automatic one is -----------
    op.add_column("cases", sa.Column("latest_manual_decision_row_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_cases_latest_manual_decision", "cases", "decisions",
        ["latest_manual_decision_row_id", "id"], ["id", "case_id"], use_alter=True,
    )
    # Backfill only what is ORDERABLE. Exactly one manual row per case is unambiguous; two or more
    # cannot be ordered at all (`decisions.id` is a random UUID hex — the very defect this closes —
    # and `decided_at` is transaction-start time, which inverts against commit order). Those stay
    # NULL and read as the explicit `unresolved_legacy_manual` state, never as a guess.
    op.execute(
        """
        UPDATE cases c SET latest_manual_decision_row_id = d.id
        FROM decisions d
        WHERE d.case_id = c.id AND d.manual
          AND (SELECT count(*) FROM decisions m WHERE m.case_id = c.id AND m.manual) = 1
        """
    )
    op.execute(
        """
        CREATE FUNCTION cases_latest_manual_pointer() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            IF NEW.manual THEN
                UPDATE public.cases SET latest_manual_decision_row_id = NEW.id
                WHERE id = NEW.case_id;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_decisions_latest_manual_pointer AFTER INSERT ON decisions "
        "FOR EACH ROW EXECUTE FUNCTION cases_latest_manual_pointer()"
    )

    # --- F2: ONE exhaustive per-kind transition matrix ------------------------------------------
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_witness_guard()")
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        DECLARE terminal boolean := OLD.status IN ('delivered','superseded');
        BEGIN
            -- ==== identity and generation are immutable in every state (017, unchanged) ========
            IF NEW.witness_generation IS DISTINCT FROM OLD.witness_generation THEN
                RAISE EXCEPTION 'witness_generation is immutable (outbox row %)', OLD.id;
            END IF;
            IF NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.case_id IS DISTINCT FROM OLD.case_id
               OR NEW.run_id IS DISTINCT FROM OLD.run_id
               OR NEW.ordering_stream IS DISTINCT FROM OLD.ordering_stream
               OR NEW.decision_sequence IS DISTINCT FROM OLD.decision_sequence THEN
                RAISE EXCEPTION 'outbox identity is immutable (outbox row %)', OLD.id;
            END IF;

            -- ==== a terminal row is FINAL (re-audit cbb783b F2) ================================
            -- Shape CHECKs constrain a row's state; only history constrains its TRANSITIONS. A
            -- legacy delivered row with a NULL digest is terminal too: finality is a property of
            -- the STATUS, never of how much witness evidence happens to exist.
            IF terminal THEN
                IF NEW.status IS DISTINCT FROM OLD.status THEN
                    RAISE EXCEPTION 'outbox row % is % and terminal; % is not reachable from it',
                        OLD.id, OLD.status, NEW.status;
                END IF;
                IF NEW.delivered_at IS DISTINCT FROM OLD.delivered_at
                   OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                   OR NEW.next_attempt_at IS DISTINCT FROM OLD.next_attempt_at
                   OR NEW.attempts IS DISTINCT FROM OLD.attempts
                   OR NEW.last_error IS DISTINCT FROM OLD.last_error
                   OR NEW.claim_token IS DISTINCT FROM OLD.claim_token
                   OR NEW.claimed_by IS DISTINCT FROM OLD.claimed_by
                   OR NEW.claim_lease_expires_at IS DISTINCT FROM OLD.claim_lease_expires_at THEN
                    RAISE EXCEPTION 'a terminal outbox row is frozen: only the governed payload '
                        'redaction may be written (outbox row %, status %)', OLD.id, OLD.status;
                END IF;
            ELSE
                -- ==== live rows: the complete forward matrix, per kind =========================
                IF OLD.kind = 'decision_callback' THEN
                    IF NOT (
                        (OLD.status = 'pending'
                         AND NEW.status IN ('pending','dead','delivered','superseded'))
                        OR (OLD.status = 'dead' AND NEW.status IN ('dead','pending'))
                    ) THEN
                        RAISE EXCEPTION 'illegal decision-callback transition % -> % '
                            '(outbox row %)', OLD.status, NEW.status, OLD.id;
                    END IF;
                ELSE
                    -- poc_email never supersedes (the lifecycle CHECK agrees)
                    IF NOT (
                        (OLD.status = 'pending' AND NEW.status IN ('pending','dead','delivered'))
                        OR (OLD.status = 'dead' AND NEW.status IN ('dead','pending'))
                    ) THEN
                        RAISE EXCEPTION 'illegal poc-email transition % -> % (outbox row %)',
                            OLD.status, NEW.status, OLD.id;
                    END IF;
                END IF;
            END IF;

            -- ==== the payload: immutable while sendable, redactable once terminal ==============
            IF NEW.payload_json IS DISTINCT FROM OLD.payload_json THEN
                IF NOT (terminal AND NEW.payload_json = '{"redacted": true}'::jsonb) THEN
                    RAISE EXCEPTION 'outbox payload is immutable while sendable; a terminal body '
                        'may only take the governed redaction value (outbox row %)', OLD.id;
                END IF;
            END IF;

            -- ==== the wire witness (017, unchanged except for its now-guaranteed source) =======
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


def downgrade() -> None:
    # F3: `017`'s downgrade restores `016`'s functions with their historical UNQUALIFIED bodies and
    # caller-controlled search_path, so a witness-free walk past this point lands on authority a
    # shadow-first search path can redirect. `017` is published and is never edited (the rule this
    # unit has followed five times), so the walk is closed here instead: once `018` is installed
    # the supported rollback is the prior compatible IMAGE on schema `018`.
    raise RuntimeError(
        f"migration 018 downgrade refused: {_FORWARD_ONLY_SENTINEL} — 018 is forward-only once "
        f"installed. Walking below it would restore search-path-vulnerable authority functions "
        f"(re-audit cbb783b F3). Roll back by deploying the prior reviewed 018-compatible image "
        f"against this schema; the schema itself stays at 018."
    )
