"""outbox poison recovery: a guard must never make a stuck row unrecoverable (PR 7b-core)

Revision ID: 021
Revises: 020

Folds an adversarial review of the released `020`, which found that my own fix was worse than the
bug it replaced.

**P1 — `020`'s new arm asserted a STATE, not a TRANSITION, so it fired on the CLAIM.**

    IF NEW.status = 'pending' AND NEW.payload_json = '{"redacted": true}'::jsonb THEN RAISE

Nothing there tests that anything is *changing*, so it refused every UPDATE that left a row
pending-and-redacted — not merely the ones that put it there. Measured against such a row: the
publisher's CLAIM refused, `_record_failure`'s retry branch refused, clearing a stale claim
refused, pushing `next_attempt_at` out refused. The only escape was `SET status='dead'`, which is
documented nowhere.

That is catastrophic rather than annoying, because `_CLAIM_SQL` runs in `process_once` with **no**
`try/except` — the handler there wraps only `_deliver` — and `run_forever` has none either. So the
exception exits the worker process; and since the claim takes the globally lowest per-stream head
by `min(id)`, once the rows ahead of it drain the poisoned row is the permanent head. The measured
result at `020` was **nothing delivered at all**, including a decision callback in a different case
and a different stream. `019`'s behaviour for the same seed was bounded and self-healing: retry,
retry, dead-letter, and the healthy sibling delivered. `020` converted a ~21-minute head-of-line
delay into an unbounded total outage of both streams.

Worse, the state it fires on is *reachable*, and `020` could not maintain the invariant it
asserted: `outbox_lifecycle_insert_guard` constrains only `kind='decision_callback'` and never
looks at the payload at all, so a plain INSERT of a pending, already-redacted row — of EITHER kind
— was accepted. The arm could therefore only ever fire on a row that already violated the
invariant, which is exactly where firing does maximum damage.

**What this revision does.**

1. **The arm becomes a transition rule.** It refuses a row BECOMING pending-and-redacted, and
   leaves an already-poisoned row claimable — so it retries, dead-letters, and drains itself the
   way `019` did. A guard's job is to stop the bad write, never to strand the row.
2. **The invariant is maintained where it belongs, at INSERT, for BOTH kinds.** No row may be born
   pending with a redacted body. `017`'s guard covered only decision callbacks and only their
   terminal/claim fields; poc_email had no INSERT guard whatsoever.
3. **The upgrade REFUSES if any poisoned row already exists**, with a stable sentinel naming the
   ids. `020` held ACCESS EXCLUSIVE and never looked — so upgrading a database damaged by a `019`
   deployment silently armed the outage instead of reporting it. `017` set the precedent that a
   precondition is machine-checked, not assumed.
4. **`kyc_set_latest_decision_row()` is recreated with a pinned `search_path` and qualified
   relations.** `014` created it with neither, and `020`'s "COMPLETE CODE SURFACE" preflight —
   which charged `019` with exactly this class of overclaim — did not pin it. Proven: gutting that
   function at `020` let `alembic upgrade head` succeed with `cases.latest_decision_row_id` left
   NULL, which is the pointer the read API and the console serve as the verdict.
"""

import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_021_AUTHORITY_CODE_MISMATCH"
_POISON_SENTINEL = "MIGRATION_021_PREFLIGHT_UNSENDABLE_PENDING_ROWS"
_FORWARD_ONLY_SENTINEL = "MIGRATION_021_DOWNGRADE_REFUSED_FORWARD_ONLY"
_FENCE_KEY = 720170001

_REDACTED = "'{\"redacted\": true}'::jsonb"

# Every function this chain owns, at the state `020` leaves them in — now including
# `kyc_set_latest_decision_row`, whose omission was `020`'s own version of the overclaim it
# charged `019` with.
_EXPECTED_FUNCTION_SHA256 = {
    "cases_latest_manual_pointer":
        "4e62e8d407efafcd994edc9f4eabef9a875e4f4c8bd280a73c80e80d703c881e",
    "kyc_set_latest_decision_row":
        "aff4d22d2f1143372be0c99b8ac1c824e9d7465eb5e7ce800e311f14d0ca6611",
    "outbox_attempts_admission":
        "6efacbd11eb8b2cc097d940013faead2e1af06adbb666b6e335cf6248bff28c7",
    "outbox_attempts_guard":
        "484829cd1546c28f3b9f887ea8e8c9fbfb9a280c2305975cff9e4fff0bc119c9",
    "outbox_lifecycle_insert_guard":
        "ff3a3bd81880de5dfc52340c9dd6e3ea3d15afb36214b62d3c5d603673f543ab",
    "outbox_no_delete_guard":
        "b683cbcd772fa5701642ee7992f6bb66105f7c9ddc911d3c2b03c3995b5109ef",
    "outbox_witness_guard":
        "c611e93f285b9b87b3c72e9de54d6836ff118740003afb946737b5af533c8100",
}
# `kyc_set_latest_decision_row` is deliberately absent: `014` created it WITHOUT a pin, so
# requiring one before this revision installs it would refuse every correct database. It is
# recreated with the pin below, and joins this set for whatever revision comes next.
_MUST_PIN_SEARCH_PATH = {
    "cases_latest_manual_pointer", "outbox_attempts_admission", "outbox_attempts_guard",
    "outbox_lifecycle_insert_guard", "outbox_no_delete_guard", "outbox_witness_guard",
}
_EXPECTED_SEARCH_PATH = "search_path=public, pg_catalog"
_EXPECTED_TRIGGERS = {
    "outbox_delivery_attempts": {"trg_outbox_attempts_admission", "trg_outbox_attempts_immutable"},
    "outbox": {"trg_outbox_lifecycle_insert", "trg_outbox_no_delete", "trg_outbox_witness_guard"},
    "decisions": {"trg_decisions_latest_pointer", "trg_decisions_latest_manual_pointer"},
}


def _body_digest(prosrc: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", prosrc).strip().encode()).hexdigest()


def _validate_code_surface(conn) -> None:
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
            problems.append(f"function {name}: body digest {got} != expected {want}")
        if name in _MUST_PIN_SEARCH_PATH and _EXPECTED_SEARCH_PATH not in proconfig:
            problems.append(
                f"function {name}: search_path not pinned to {_EXPECTED_SEARCH_PATH!r} "
                f"(found {proconfig!r})")

    for rel, expected in _EXPECTED_TRIGGERS.items():
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
                f"{rel} enabled triggers: expected exactly {sorted(expected)}, found {sorted(got)}")

    if problems:
        raise RuntimeError(
            f"migration 021: {_MISMATCH_SENTINEL} — the outbox authority CODE surface is not the "
            f"one revision 020 left installed; refusing to replace it. Problems: {problems}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))
    _validate_code_surface(conn)

    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")

    # A row that is already pending-and-redacted is unsendable: its body is gone, so every claim
    # ends in failure. Under `020` it also KILLED the publisher and, being the min(id) head,
    # stopped every other stream. `020` never looked for these — it held the table locked and
    # armed the outage in silence. This revision refuses and names them, because an operator can
    # dead-letter them in one statement and cannot debug a worker that exits without a message.
    poisoned = conn.execute(sa.text(
        f"SELECT id, kind, case_id FROM outbox WHERE status = 'pending' "
        f"AND payload_json = {_REDACTED} ORDER BY id")).fetchall()
    if poisoned:
        listed = ", ".join(f"id={r.id} ({r.kind}, case {r.case_id})" for r in poisoned)
        raise RuntimeError(
            f"migration 021: {_POISON_SENTINEL} — {len(poisoned)} pending outbox row(s) carry a "
            f"redacted body and can never be delivered: {listed}. They are unrecoverable by "
            f"design (the body is destroyed), so retire them before upgrading:\n"
            f"    UPDATE outbox SET status='dead', last_error='body redacted; unsendable' "
            f"WHERE status='pending' AND payload_json = {_REDACTED};\n"
            f"For a decision callback, re-emit via recalculate.requested; for a poc_email, "
            f"re-submit the POC. This revision refuses rather than leaving them to stall the "
            f"claim path."
        )

    # --- the arm becomes a TRANSITION rule -----------------------------------------------------
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_witness_guard()")
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        DECLARE terminal boolean := OLD.status IN ('delivered','superseded');
        BEGIN
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
                IF OLD.status = 'pending' THEN
                    RAISE EXCEPTION 'outbox payload is immutable while the row is pending '
                        '(outbox row %)', OLD.id;
                END IF;
            END IF;
            -- A TRANSITION rule, not a state assertion (revision 021). 020 omitted the
            -- "is anything changing?" test, so this fired on the CLAIM of a row that was already
            -- pending+redacted — and since the claim has no exception handler and takes the
            -- min(id) head, that exit stopped every stream. An already-poisoned row must stay
            -- claimable so it can fail, dead-letter, and drain itself; what must be refused is a
            -- scrubbed body BECOMING sendable.
            IF NEW.status = 'pending' AND NEW.payload_json = '{"redacted": true}'::jsonb
               AND NOT (OLD.status = 'pending'
                        AND OLD.payload_json = '{"redacted": true}'::jsonb) THEN
                RAISE EXCEPTION 'a redacted outbox row can never be MADE sendable again: its body '
                    'is gone, so it would be claimed and fail forever (outbox row %). Re-emit the '
                    'decision, or re-submit the POC, instead.', OLD.id;
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

    # --- the invariant is maintained at INSERT, for BOTH kinds ---------------------------------
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_lifecycle_insert ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_lifecycle_insert_guard()")
    op.execute(
        """
        CREATE FUNCTION outbox_lifecycle_insert_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            -- BOTH kinds (revision 021): a row born pending with a destroyed body can never be
            -- delivered, and until 021 nothing stopped one being inserted. poc_email had no
            -- INSERT guard at all, and the decision-callback guard never looked at the payload.
            IF NEW.status = 'pending' AND NEW.payload_json = '{"redacted": true}'::jsonb THEN
                RAISE EXCEPTION 'an outbox row cannot be born pending with a redacted body: it '
                    'would be claimed and fail forever (kind %, case %)', NEW.kind, NEW.case_id;
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

    # --- the last unpinned authority function --------------------------------------------------
    # `014` created this with no `SET search_path` and an unqualified `UPDATE cases`, and `020`'s
    # "complete code surface" preflight did not pin it — the same overclaim `020` charged `019`
    # with. It maintains `cases.latest_decision_row_id`, which the read API and the console serve
    # as THE verdict, so a redirected or gutted body silently blanks the authority row.
    op.execute("DROP TRIGGER IF EXISTS trg_decisions_latest_pointer ON decisions")
    op.execute("DROP FUNCTION IF EXISTS kyc_set_latest_decision_row()")
    op.execute(
        """
        CREATE FUNCTION kyc_set_latest_decision_row() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        BEGIN
            UPDATE public.cases SET latest_decision_row_id = NEW.id WHERE id = NEW.case_id;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_decisions_latest_pointer AFTER INSERT ON decisions "
        "FOR EACH ROW EXECUTE FUNCTION kyc_set_latest_decision_row()"
    )


def downgrade() -> None:
    raise RuntimeError(
        f"migration 021 downgrade refused: {_FORWARD_ONLY_SENTINEL} — 021 is forward-only, for "
        f"the same reason 018, 019 and 020 are. It would additionally reinstate a guard that "
        f"refuses to CLAIM an already-unsendable row, which stops the whole outbox rather than "
        f"letting that row dead-letter itself. Roll back by deploying the prior reviewed "
        f"021-compatible image against this schema."
    )
