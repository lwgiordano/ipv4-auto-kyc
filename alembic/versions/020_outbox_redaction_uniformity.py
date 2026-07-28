"""outbox redaction uniformity: no sendable body may be scrubbed, no scrubbed body may be sent

Revision ID: 020
Revises: 019

Folds an adversarial review of the released `019`. That review's headline was good — a byte-level
`prosrc` diff and a 363-cell behavioural differential proved `019`'s recreation lost nothing of
`018` — but it found two real defects in the new payload rule and one in the new preflight.

**1. The rule keyed only on `OLD.status`, so a scrubbed body could become sendable again.** The
stated invariant was "a body that can still be sent is immutable"; the implementation asked only
what the row WAS, never what it was BECOMING. Both of these were accepted:

    UPDATE outbox SET status='pending', payload_json='{"redacted": true}'::jsonb, attempts=0 …
    -- and, in two statements, the second not even entering the payload branch:
    UPDATE outbox SET payload_json='{"redacted": true}'::jsonb WHERE id=<dead poc>;
    UPDATE outbox SET status='pending', attempts=0 WHERE id=<same row>;

The result is a `pending` POC email with no `to`/`body`. `_CLAIM_SQL` takes the per-stream FIFO
head by `min(id)`, so the poisoned row is claimed FIRST and `_deliver` raises `KeyError: 'to'` —
head-of-line blocking every later POC email of that case for the whole backoff schedule (~21
minutes on production defaults), and the body can never be restored because a `pending` payload is
immutable. `018` refused both writes only because it refused the `dead` redaction outright; `019`
opened the door and did not put a lock on the other side. The rule now names both ends: a body may
not be scrubbed while it is sendable, and a scrubbed body may not become sendable.

**2. The `dead` decision-callback exception was a permanent carve-out with no expiry, and it
foreclosed a retention obligation.** `retention.py` states that a callback body carries
`checks[].source`, which can be reviewer-derived, and is therefore destroyed past the window. Its
SQL only reaches `delivered`/`superseded`, so a `dead` callback — the ordinary outcome after
`outbox_max_attempts` platform failures — kept a reviewer identifier forever, and `019`'s named
exception made widening that SQL impossible at the DDL layer.

This revision removes the exception instead of dating it. The redaction is now uniform: permitted
on any row that is neither currently nor becoming `pending`, for both kinds. What made the
exception seem necessary — "a dead callback must stay requeueable" — is answered where it belongs,
at the requeue endpoint, which already refused a redacted POC email and now refuses a redacted
callback for the same reason and with the same remedy: the body is gone, so re-emit the decision
rather than re-sending a scrubbed one. Retention then widens to dead callbacks, aged on
`created_at` (a dead row has neither `delivered_at` nor `resolved_at`).

**3. The preflight claimed `018`'s discipline and checked one function.** `019`'s docstring said
"same discipline as `018`'s manifest" while validating only `outbox_witness_guard`'s digest and
`search_path`. Thirteen of the fourteen drifts `018` refuses walked straight through `019` and got
stamped as a fresh revision — including `RESET search_path` on the other four owned functions (the
exact shadow-relation exposure the forward-only rule exists to prevent), a DISABLED delete guard,
and an unexpected extra trigger that survives a recreation naming only one trigger by name.

This revision validates the COMPLETE CODE SURFACE it is responsible for — every owned function's
normalized-body digest and pinned `search_path`, plus the complete enabled trigger set of both
authority tables — and says exactly that. It deliberately does NOT re-validate the data-shape
surface (columns, constraints, indexes): `018` validated those when it ran, this revision changes
no data shape, and claiming to check them again is how `019` came to overstate its own scope.
"""

import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_020_AUTHORITY_CODE_MISMATCH"
_FORWARD_ONLY_SENTINEL = "MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY"
_FENCE_KEY = 720170001

# Every function this chain owns, at the state `019` leaves them in. `outbox_witness_guard` is the
# one being replaced here; the rest must be untouched, or the environment is not the one this
# repair was written against.
_EXPECTED_FUNCTION_SHA256 = {
    "cases_latest_manual_pointer":
        "4e62e8d407efafcd994edc9f4eabef9a875e4f4c8bd280a73c80e80d703c881e",
    "outbox_attempts_admission":
        "6efacbd11eb8b2cc097d940013faead2e1af06adbb666b6e335cf6248bff28c7",
    "outbox_attempts_guard":
        "484829cd1546c28f3b9f887ea8e8c9fbfb9a280c2305975cff9e4fff0bc119c9",
    "outbox_lifecycle_insert_guard":
        "ff3a3bd81880de5dfc52340c9dd6e3ea3d15afb36214b62d3c5d603673f543ab",
    "outbox_no_delete_guard":
        "b683cbcd772fa5701642ee7992f6bb66105f7c9ddc911d3c2b03c3995b5109ef",
    "outbox_witness_guard":
        "4be7ca7104359fe939b668eed44930da60e77ced9f62d164733c672a20fde4fe",
}
_EXPECTED_SEARCH_PATH = "search_path=public, pg_catalog"
_EXPECTED_TRIGGERS = {
    "outbox_delivery_attempts": {"trg_outbox_attempts_admission", "trg_outbox_attempts_immutable"},
    "outbox": {"trg_outbox_lifecycle_insert", "trg_outbox_no_delete", "trg_outbox_witness_guard"},
}
_EXPECTED_DECISIONS_TRIGGERS = {"trg_decisions_latest_pointer", "trg_decisions_latest_manual_pointer"}


def _body_digest(prosrc: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", prosrc).strip().encode()).hexdigest()


def _validate_code_surface(conn) -> None:
    """Every owned function and every enabled trigger — the complete surface this revision is
    responsible for. NOT the data shape: `018` validated columns/constraints/indexes when it ran,
    nothing here changes them, and re-claiming that check is precisely how `019` overstated its
    own scope (the review found 13 of 14 drifts walking through it)."""
    problems: list[str] = []

    procs = {
        r.proname: (r.prosrc, list(r.proconfig or []))
        for r in conn.execute(sa.text(
            "SELECT p.proname, p.prosrc, p.proconfig FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname = ANY(:names)"),
            {"names": sorted(_EXPECTED_FUNCTION_SHA256)})
    }
    for name, want in _EXPECTED_FUNCTION_SHA256.items():
        found = procs.get(name)
        if found is None:
            problems.append(f"function {name}: missing")
            continue
        prosrc, proconfig = found
        got = _body_digest(prosrc)
        if got != want:
            problems.append(
                f"function {name}: body digest {got} != expected {want} (not the definition "
                f"revision 019 left installed)")
        if _EXPECTED_SEARCH_PATH not in proconfig:
            problems.append(
                f"function {name}: search_path not pinned to {_EXPECTED_SEARCH_PATH!r} "
                f"(found {proconfig!r}) — an unpinned authority function can be redirected at "
                f"runtime by a shadow-first search path")

    for rel, expected in list(_EXPECTED_TRIGGERS.items()) + [
        ("decisions", _EXPECTED_DECISIONS_TRIGGERS)
    ]:
        oid = conn.execute(sa.text(
            "SELECT to_regclass(format('%I.%I', current_schema(), CAST(:rel AS text)))::oid"),
            {"rel": rel}).scalar()
        if oid is None:
            problems.append(f"{rel}: relation missing")
            continue
        got = {
            r.tgname for r in conn.execute(sa.text(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = :oid AND NOT tgisinternal "
                "AND tgenabled <> 'D'"), {"oid": oid})
        }
        if got != expected:
            problems.append(
                f"{rel} enabled triggers: expected exactly {sorted(expected)}, found "
                f"{sorted(got)} (a DISABLED guard and an unexpected extra trigger are both "
                f"drift this revision must not build on top of)")

    if problems:
        raise RuntimeError(
            f"migration 020: {_MISMATCH_SENTINEL} — the outbox authority CODE surface is not the "
            f"one revision 019 left installed; refusing to replace it. Reconcile the environment "
            f"(or restore the reviewed image) and retry. Problems: {problems}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))
    _validate_code_surface(conn)

    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_witness_guard()")
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        DECLARE terminal boolean := OLD.status IN ('delivered','superseded');
        BEGIN
            -- ==== identity is immutable in every state (019, unchanged) =======================
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

            -- ==== a terminal row is FINAL (018, unchanged) ====================================
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
                -- ==== live rows: the complete forward matrix, per kind (018, unchanged) =======
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

            -- ==== the payload: BOTH ends of "sendable" (revision 020) =========================
            -- A body may not be scrubbed while it is sendable, AND a scrubbed body may not
            -- become sendable. 019 stated the first and implemented only half of it: keying on
            -- OLD.status alone let `dead + redact + reopen` produce a pending row with no
            -- recipient, claimed FIRST by min(id) and blocking its whole stream. The rule is now
            -- uniform across kinds — no `dead`-callback carve-out — because "a dead callback must
            -- stay requeueable" belongs at the requeue endpoint, which refuses a redacted row of
            -- either kind, not in a DDL exception that outlives the retention obligation.
            IF NEW.payload_json IS DISTINCT FROM OLD.payload_json THEN
                IF NEW.payload_json <> '{"redacted": true}'::jsonb THEN
                    RAISE EXCEPTION 'outbox payload may only be replaced by the governed '
                        'redaction value (outbox row %, status %)', OLD.id, OLD.status;
                END IF;
                IF OLD.status = 'pending' THEN
                    RAISE EXCEPTION 'outbox payload is immutable while the row is pending '
                        '(outbox row %)', OLD.id;
                END IF;
            END IF;
            IF NEW.status = 'pending'
               AND NEW.payload_json = '{"redacted": true}'::jsonb THEN
                RAISE EXCEPTION 'a redacted outbox row can never be made sendable again: its body '
                    'is gone, so it would be claimed and fail forever (outbox row %). Re-emit the '
                    'decision, or re-submit the POC, instead.', OLD.id;
            END IF;

            -- ==== the wire witness (018, unchanged) ===========================================
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
    raise RuntimeError(
        f"migration 020 downgrade refused: {_FORWARD_ONLY_SENTINEL} — 020 is forward-only, for "
        f"the same reason 018 and 019 are: walking below it reaches 017's downgrade, which "
        f"restores search-path-vulnerable authority functions. It would additionally reinstate a "
        f"payload rule under which a scrubbed body can be made sendable again. Roll back by "
        f"deploying the prior reviewed 020-compatible image against this schema."
    )
