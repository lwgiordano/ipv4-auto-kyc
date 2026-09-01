"""outbox authority manifest + terminal invariants (owner audit of 018-021)

Revision ID: 022
Revises: 021

Repairs owner-audit findings against the released 018-021 range without editing any frozen
published revision.

* 020/021 checked trigger names only, so a same-name trigger that pointed at a no-op function, or
  a trigger switched to replica-only mode, was accepted and stamped as fresh authority. This
  revision validates exact trigger definitions plus origin-enable mode before any replacement.
* POC email rows could become terminal while keeping the raw token body. Terminal POC rows are
  redacted before the new guard is installed, and every future terminal transition must redact in
  the same statement.
* `created_at` drives dead-callback retention and was still mutable on nonterminal rows. It is now
  immutable in every status.
* `cases.latest_manual_decision_row_id` could be pointed at a same-case automatic decision. The
  pointer is now DB-bound to `decisions.manual = true`, and `decisions.manual` is immutable.
"""

import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_022_AUTHORITY_SURFACE_MISMATCH"
_MANUAL_POINTER_SENTINEL = "MIGRATION_022_MANUAL_POINTER_MISMATCH"
_FORWARD_ONLY_SENTINEL = "MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY"
_FENCE_KEY = 720170001
_REDACTED = "'{\"redacted\": true}'::jsonb"

_EXPECTED_SEARCH_PATH = "search_path=public, pg_catalog"

_EXPECTED_FUNCTION_SHA256 = {
    "cases_latest_manual_pointer":
        "4e62e8d407efafcd994edc9f4eabef9a875e4f4c8bd280a73c80e80d703c881e",
    "kyc_set_latest_decision_row":
        "e54ede0f005244a727c34fe76555b89b472209fa93c4ef86a7bfe3846655cce5",
    "outbox_attempts_admission":
        "6efacbd11eb8b2cc097d940013faead2e1af06adbb666b6e335cf6248bff28c7",
    "outbox_attempts_guard":
        "484829cd1546c28f3b9f887ea8e8c9fbfb9a280c2305975cff9e4fff0bc119c9",
    "outbox_lifecycle_insert_guard":
        "50f118c36288717548c996da553f753e990e1a0ccd39313e714eeb3721e1a0ab",
    "outbox_no_delete_guard":
        "b683cbcd772fa5701642ee7992f6bb66105f7c9ddc911d3c2b03c3995b5109ef",
    "outbox_witness_guard":
        "a427a3159e7637a013a4640e7577be1f712a1da28a5f4d29e0e898cc05cb065f",
}

_EXPECTED_TRIGGERS = {
    "outbox_delivery_attempts": {
        "trg_outbox_attempts_admission": (
            "O",
            "CREATE TRIGGER trg_outbox_attempts_admission BEFORE INSERT ON "
            "public.outbox_delivery_attempts FOR EACH ROW EXECUTE FUNCTION "
            "outbox_attempts_admission()",
        ),
        "trg_outbox_attempts_immutable": (
            "O",
            "CREATE TRIGGER trg_outbox_attempts_immutable BEFORE DELETE OR UPDATE ON "
            "public.outbox_delivery_attempts FOR EACH ROW EXECUTE FUNCTION "
            "outbox_attempts_guard()",
        ),
    },
    "outbox": {
        "trg_outbox_lifecycle_insert": (
            "O",
            "CREATE TRIGGER trg_outbox_lifecycle_insert BEFORE INSERT ON public.outbox "
            "FOR EACH ROW EXECUTE FUNCTION outbox_lifecycle_insert_guard()",
        ),
        "trg_outbox_no_delete": (
            "O",
            "CREATE TRIGGER trg_outbox_no_delete BEFORE DELETE ON public.outbox "
            "FOR EACH ROW EXECUTE FUNCTION outbox_no_delete_guard()",
        ),
        "trg_outbox_witness_guard": (
            "O",
            "CREATE TRIGGER trg_outbox_witness_guard BEFORE UPDATE ON public.outbox "
            "FOR EACH ROW EXECUTE FUNCTION outbox_witness_guard()",
        ),
    },
    "decisions": {
        "trg_decisions_latest_manual_pointer": (
            "O",
            "CREATE TRIGGER trg_decisions_latest_manual_pointer AFTER INSERT ON public.decisions "
            "FOR EACH ROW EXECUTE FUNCTION cases_latest_manual_pointer()",
        ),
        "trg_decisions_latest_pointer": (
            "O",
            "CREATE TRIGGER trg_decisions_latest_pointer AFTER INSERT ON public.decisions "
            "FOR EACH ROW EXECUTE FUNCTION kyc_set_latest_decision_row()",
        ),
    },
}


def _body_digest(prosrc: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", prosrc).strip().encode()).hexdigest()


def _validate_authority_surface(conn) -> None:
    problems: list[str] = []
    procs = {
        r.proname: (r.prosrc, list(r.proconfig or []))
        for r in conn.execute(
            sa.text(
                "SELECT p.proname, p.prosrc, p.proconfig FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = current_schema() AND p.proname = ANY(:names)"
            ),
            {"names": sorted(_EXPECTED_FUNCTION_SHA256)},
        )
    }
    for name, want in _EXPECTED_FUNCTION_SHA256.items():
        found = procs.get(name)
        if found is None:
            problems.append(f"function {name}: missing")
            continue
        prosrc, proconfig = found
        got = _body_digest(prosrc)
        if got != want:
            problems.append(f"function {name}: body digest {got} != expected {want}")
        if _EXPECTED_SEARCH_PATH not in proconfig:
            problems.append(
                f"function {name}: search_path not pinned to {_EXPECTED_SEARCH_PATH!r} "
                f"(found {proconfig!r})"
            )

    for rel, expected in _EXPECTED_TRIGGERS.items():
        oid = conn.execute(
            sa.text("SELECT to_regclass(format('%I.%I', current_schema(), CAST(:rel AS text)))::oid"),
            {"rel": rel},
        ).scalar()
        if oid is None:
            problems.append(f"{rel}: relation missing")
            continue
        got = {
            r.tgname: (r.tgenabled, r.tdef)
            for r in conn.execute(
                sa.text(
                    "SELECT tgname, tgenabled, pg_get_triggerdef(oid) AS tdef "
                    "FROM pg_trigger WHERE tgrelid=:oid AND NOT tgisinternal"
                ),
                {"oid": oid},
            )
        }
        if set(got) != set(expected):
            problems.append(
                f"{rel} triggers: expected exactly {sorted(expected)}, found {sorted(got)}"
            )
            continue
        for name, (want_mode, want_def) in expected.items():
            mode, definition = got[name]
            if mode != want_mode:
                problems.append(
                    f"{rel} trigger {name}: mode {mode} != expected {want_mode} "
                    "(origin-enabled authority)"
                )
            if definition != want_def:
                problems.append(
                    f"{rel} trigger {name}: definition {definition!r} != expected {want_def!r}"
                )

    if problems:
        raise RuntimeError(
            f"migration 022: {_MISMATCH_SENTINEL} — the outbox authority surface is not the "
            f"one revision 021 left installed; refusing to build on it. Problems: {problems}"
        )


def _validate_manual_pointer(conn) -> None:
    bad = conn.execute(
        sa.text(
            "SELECT c.id, c.latest_manual_decision_row_id FROM cases c "
            "JOIN decisions d ON d.id = c.latest_manual_decision_row_id "
            "WHERE c.latest_manual_decision_row_id IS NOT NULL AND d.manual IS NOT TRUE "
            "ORDER BY c.id LIMIT 20"
        )
    ).fetchall()
    if bad:
        listed = ", ".join(
            f"case={r.id} pointer={r.latest_manual_decision_row_id}" for r in bad
        )
        raise RuntimeError(
            f"migration 022: {_MANUAL_POINTER_SENTINEL} — latest manual pointers must "
            f"reference manual decision rows before the guard can be installed: {listed}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))
    _validate_authority_surface(conn)

    op.execute("LOCK TABLE outbox_delivery_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE cases IN ACCESS EXCLUSIVE MODE")
    _validate_manual_pointer(conn)

    # Terminal POC rows that were valid before this revision are repaired before the stricter
    # guard is installed. A pending redacted POC remains refused by 021's preflight before 022 can
    # run; this covers rows that already reached a terminal state while still carrying a token.
    op.execute(
        f"UPDATE outbox SET payload_json={_REDACTED} "
        f"WHERE kind='poc_email' AND status IN ('dead','delivered') "
        f"AND payload_json <> {_REDACTED}"
    )

    op.execute("DROP TRIGGER IF EXISTS trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_witness_guard()")
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        DECLARE terminal boolean := OLD.status IN ('delivered','superseded');
        BEGIN
            IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'created_at is immutable (outbox row %)', OLD.id;
            END IF;
            IF NEW.witness_generation IS DISTINCT FROM OLD.witness_generation THEN
                RAISE EXCEPTION 'witness_generation is immutable (outbox row %)', OLD.id;
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.case_id IS DISTINCT FROM OLD.case_id
               OR NEW.run_id IS DISTINCT FROM OLD.run_id
               OR NEW.ordering_stream IS DISTINCT FROM OLD.ordering_stream
               OR NEW.decision_sequence IS DISTINCT FROM OLD.decision_sequence THEN
                RAISE EXCEPTION 'outbox identity is immutable (outbox row %)', OLD.id;
            END IF;

            IF terminal THEN
                IF NEW.status IS DISTINCT FROM OLD.status THEN
                    RAISE EXCEPTION 'outbox row % is % and terminal; % is not reachable from it',
                        OLD.id, OLD.status, NEW.status;
                END IF;
                IF NEW.delivered_at IS DISTINCT FROM OLD.delivered_at
                   OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at
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
                    IF NOT (
                        (OLD.status = 'pending' AND NEW.status IN ('pending','dead','delivered'))
                        OR (OLD.status = 'dead' AND NEW.status IN ('dead','pending'))
                    ) THEN
                        RAISE EXCEPTION 'illegal poc-email transition % -> % (outbox row %)',
                            OLD.status, NEW.status, OLD.id;
                    END IF;
                END IF;
            END IF;

            IF NEW.payload_json IS DISTINCT FROM OLD.payload_json THEN
                IF NEW.payload_json <> '{"redacted": true}'::jsonb THEN
                    RAISE EXCEPTION 'outbox payload may only be replaced by the governed '
                        'redaction value (outbox row %, status %)', OLD.id, OLD.status;
                END IF;
                IF OLD.status = 'pending' AND NOT (
                    OLD.kind = 'poc_email' AND NEW.status IN ('dead','delivered')
                ) THEN
                    RAISE EXCEPTION 'outbox payload is immutable while the row is pending '
                        '(outbox row %)', OLD.id;
                END IF;
            END IF;
            IF NEW.status = 'pending' AND NEW.payload_json = '{"redacted": true}'::jsonb
               AND NOT (OLD.status = 'pending'
                        AND OLD.payload_json = '{"redacted": true}'::jsonb) THEN
                RAISE EXCEPTION 'a redacted outbox row can never be MADE sendable again: its body '
                    'is gone, so it would be claimed and fail forever (outbox row %). Re-emit the '
                    'decision, or re-submit the POC, instead.', OLD.id;
            END IF;

            IF OLD.kind = 'poc_email' AND OLD.status = 'pending'
               AND NEW.status IN ('dead','delivered')
               AND NEW.payload_json <> '{"redacted": true}'::jsonb THEN
                RAISE EXCEPTION 'poc_email terminal rows must be redacted in the terminal '
                    'transition (outbox row %)', OLD.id;
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

    op.execute("DROP TRIGGER IF EXISTS trg_outbox_lifecycle_insert ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_lifecycle_insert_guard()")
    op.execute(
        """
        CREATE FUNCTION outbox_lifecycle_insert_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            IF NEW.status = 'pending' AND NEW.payload_json = '{"redacted": true}'::jsonb THEN
                RAISE EXCEPTION 'an outbox row cannot be born pending with a redacted body: it '
                    'would be claimed and fail forever (kind %, case %)', NEW.kind, NEW.case_id;
            END IF;
            IF NEW.kind = 'poc_email' AND NEW.status IN ('dead','delivered')
               AND NEW.payload_json <> '{"redacted": true}'::jsonb THEN
                RAISE EXCEPTION 'poc_email terminal rows must be born redacted '
                    '(kind %, case %)', NEW.kind, NEW.case_id;
            END IF;
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

    op.execute("DROP TRIGGER IF EXISTS trg_cases_latest_manual_guard ON cases")
    op.execute("DROP FUNCTION IF EXISTS cases_latest_manual_pointer_guard()")
    op.execute(
        """
        CREATE FUNCTION cases_latest_manual_pointer_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            IF NEW.latest_manual_decision_row_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.decisions d
                WHERE d.id = NEW.latest_manual_decision_row_id
                  AND d.case_id = NEW.id
                  AND d.manual IS TRUE
            ) THEN
                RAISE EXCEPTION 'latest_manual_decision_row_id must reference manual decision '
                    'for this case (case %, decision %)',
                    NEW.id, NEW.latest_manual_decision_row_id;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_cases_latest_manual_guard "
        "BEFORE UPDATE OF latest_manual_decision_row_id ON cases "
        "FOR EACH ROW EXECUTE FUNCTION cases_latest_manual_pointer_guard()"
    )

    op.execute("DROP TRIGGER IF EXISTS trg_decisions_manual_flag_guard ON decisions")
    op.execute("DROP FUNCTION IF EXISTS decisions_manual_flag_guard()")
    op.execute(
        """
        CREATE FUNCTION decisions_manual_flag_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            IF NEW.manual IS DISTINCT FROM OLD.manual THEN
                RAISE EXCEPTION 'decision manual flag is immutable (decision %)', OLD.id;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_decisions_manual_flag_guard BEFORE UPDATE OF manual ON decisions "
        "FOR EACH ROW EXECUTE FUNCTION decisions_manual_flag_guard()"
    )


def downgrade() -> None:
    raise RuntimeError(
        f"migration 022 downgrade refused: {_FORWARD_ONLY_SENTINEL} — 022 is forward-only because "
        f"it installs stricter authority validation and terminal POC-token redaction invariants. "
        f"Roll back by deploying a reviewed 022-compatible image against this schema."
    )
