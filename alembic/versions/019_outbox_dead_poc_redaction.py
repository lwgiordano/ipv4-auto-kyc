"""outbox payload rule repair: a dead POC email must still scrub its token (PR 7b-core)

Revision ID: 019
Revises: 018

A latent defect introduced by `017`'s payload rule and inherited unchanged by `018`, found by
self-review after `018` was released and confirmed against a real publisher run.

**What was broken.** The payload rule permitted the governed redaction only when
`OLD.status IN ('delivered','superseded')`. The publisher scrubs a POC email's raw token in TWO
places — after a successful delivery (terminal, permitted) and when the row gives up and goes
`dead` (`publisher._record_failure`). The second write happens in the same transaction, one
statement after the row became `dead`, so `OLD.status` is `'dead'` — not terminal by the rule —
and the trigger refused it.

The consequences were worse than a refused UPDATE, because nothing caught the exception:

1. the refusal propagated out of `_record_failure`, so the whole terminal transaction rolled back:
   **the row never reached `dead` at all** and kept its claim history;
2. **the raw POC token stayed at rest indefinitely** — the exact outcome the scrub exists to
   prevent (`AUDIT_FINDINGS` fix #2), on the one path where the token is provably never going to
   be delivered;
3. `_record_failure` is called from `process_once`'s own `except` handler, so the raise escaped
   the outbox worker's loop and the row was retried into the same crash forever.

**The rule this revision installs.** The governed redaction value is permitted on any row that is
no longer `pending`, with one deliberate exception: a `dead` **decision callback** keeps its body,
because `dead → pending` is a supported operator requeue and the callback must still be sendable
(`ui/routes.py::requeue_outbox`). A `dead` POC email is the opposite case — the requeue endpoint
explicitly refuses a redacted one and directs the operator to re-submit the POC for a fresh token —
so scrubbing it destroys nothing recoverable. Everything else `018` froze stays frozen: a terminal
row's status, timestamps, claim tuple, witness, identity, attempt counter and error text are all
still immutable, and a `pending` body still cannot change at all.

**Also folded here (same function, same recreation).** `outbox.id` joins the immutable-identity
block. It is the ordering authority the rest of the system actually reads — retention documents
"id = order", `013`'s legacy `decision_sequence` backfill orders by it, and `_CLAIM_SQL` selects
the per-stream FIFO head with `min(id)` — yet `017` and `018` froze every other identity field and
left this one rewritable, so a row's position in the order could be rewritten in place. The child
FK is not a backstop: retention deletes attempt rows once a callback carries a digest.

`018` is published and is therefore not edited (the rule this unit has followed seven times); this
revision recreates the one function that was wrong. Like `018`, it is forward-only.
"""

import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None

_MISMATCH_SENTINEL = "MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH"
_FORWARD_ONLY_SENTINEL = "MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY"
_FENCE_KEY = 720170001

# The body `018` installed, which this revision replaces. Pinned so the repair refuses to run on
# top of a witness guard that is not the one it is repairing (same discipline as `018`'s manifest;
# it proves the OBSERVED PRESENT definition and claims nothing about earlier execution).
_EXPECTED_018_WITNESS_GUARD_SHA256 = (
    "1eecc002562fe022a58aaebbbce88e9fc5a3ec75d95b15af24b03dd37c5a878e"
)
_EXPECTED_SEARCH_PATH = "search_path=public, pg_catalog"


def _body_digest(prosrc: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", prosrc).strip().encode()).hexdigest()


def upgrade() -> None:
    conn = op.get_bind()
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))

    found = conn.execute(sa.text(
        "SELECT p.prosrc, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND p.proname = 'outbox_witness_guard'")).first()
    if found is None:
        raise RuntimeError(
            f"migration 019: {_MISMATCH_SENTINEL} — outbox_witness_guard() is missing; the "
            f"authority this revision repairs is not installed."
        )
    got_sha = _body_digest(found.prosrc)
    if got_sha != _EXPECTED_018_WITNESS_GUARD_SHA256:
        raise RuntimeError(
            f"migration 019: {_MISMATCH_SENTINEL} — outbox_witness_guard() body digest {got_sha} "
            f"is not the one revision 018 installed "
            f"({_EXPECTED_018_WITNESS_GUARD_SHA256}); refusing to replace an authority function "
            f"this revision did not write. Reconcile the environment and retry."
        )
    if _EXPECTED_SEARCH_PATH not in list(found.proconfig or []):
        raise RuntimeError(
            f"migration 019: {_MISMATCH_SENTINEL} — outbox_witness_guard() search_path is not "
            f"pinned to {_EXPECTED_SEARCH_PATH!r} (found {found.proconfig!r})."
        )

    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_witness_guard ON outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_witness_guard()")
    op.execute(
        """
        CREATE FUNCTION outbox_witness_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
        DECLARE terminal boolean := OLD.status IN ('delivered','superseded');
        BEGIN
            -- ==== identity and generation are immutable in every state ========================
            IF NEW.witness_generation IS DISTINCT FROM OLD.witness_generation THEN
                RAISE EXCEPTION 'witness_generation is immutable (outbox row %)', OLD.id;
            END IF;
            -- `id` joins the identity block in 019: it is not decoration, it IS the order the
            -- rest of the system reads. Retention documents "id = order", 013's legacy
            -- decision_sequence backfill orders by it, and _CLAIM_SQL picks the per-stream FIFO
            -- head with min(id). 017/018 froze every OTHER identity field and left this one
            -- rewritable, so the ordering authority could be re-sequenced in place.
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

            -- ==== the payload (revision 019) ==================================================
            -- A body that can still be sent is immutable. The ONLY permitted change is to the
            -- governed redaction value, and only once the row is no longer `pending` — with one
            -- exception: a DEAD DECISION CALLBACK keeps its body, because `dead -> pending` is a
            -- supported operator requeue and the callback must remain sendable. A dead POC email
            -- is the opposite case: its raw token is provably never going to be delivered, the
            -- requeue endpoint refuses a redacted one and directs the operator to re-submit, and
            -- leaving the token at rest is the very outcome the scrub exists to prevent.
            IF NEW.payload_json IS DISTINCT FROM OLD.payload_json THEN
                IF NEW.payload_json <> '{"redacted": true}'::jsonb THEN
                    RAISE EXCEPTION 'outbox payload may only be replaced by the governed '
                        'redaction value (outbox row %, status %)', OLD.id, OLD.status;
                END IF;
                IF OLD.status = 'pending' THEN
                    RAISE EXCEPTION 'outbox payload is immutable while the row is pending '
                        '(outbox row %)', OLD.id;
                END IF;
                IF OLD.status = 'dead' AND OLD.kind = 'decision_callback' THEN
                    RAISE EXCEPTION 'a dead decision callback keeps its body so it can be '
                        'requeued and re-sent (outbox row %)', OLD.id;
                END IF;
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
        f"migration 019 downgrade refused: {_FORWARD_ONLY_SENTINEL} — 019 is forward-only, for "
        f"the same reason 018 is: walking below it reaches 017's downgrade, which restores "
        f"search-path-vulnerable authority functions. It would additionally reinstate a payload "
        f"rule under which a dead POC email cannot scrub its raw token. Roll back by deploying "
        f"the prior reviewed 019-compatible image against this schema."
    )
